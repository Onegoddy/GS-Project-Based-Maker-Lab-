"""Serial compartilhada: um único leitor, telemetria, LEDs e cabeça.

v4 anuncia capacidades e envia TEL:LDR:ADC:MILLIS a cada 500 ms.
PRONTO/v2/v3 e consulta digital L continuam compatíveis com os efeitos antigos.
Nenhum consumidor lê a porta: usam apenas o cache imutável de telemetria.
"""

import re
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import contextmanager
from .config import obter_variavel

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = list_ports = None

FABRICANTES_USB = {0x2341: "Arduino", 0x2A03: "Arduino", 0x1A86: "CH340 (clone)",
                   0x0403: "FTDI", 0x10C4: "CP210x"}
NOMES_TIPICOS = ("usbmodem", "usbserial", "wchusbserial", "ttyACM", "ttyUSB")
PAUSA_INVERSAO_MS = 120
AMPLITUDE_SKETCH_MS = 600
BALANCO_PADRAO_MS = 60


@dataclass(frozen=True)
class LeituraLDR:
    bruto: int
    instante: float
    recebido_em: datetime
    millis_placa: int | None
    sequencia: int
    tipo: str = "analogico"


class Arduino:
    OLHOS_ACENDER = b"O"
    OLHOS_APAGAR = b"o"
    OLHO_VERMELHO = b"V"
    OLHO_AZUL = b"A"
    CABECA_GIRAR = b"G"
    CABECA_PARAR = b"P"
    SENSOR_LUZ_LER = b"L"

    def __init__(self, porta=None, baudrate=9600, ativo=True, *, fabrica_serial=None,
                 relogio=time.monotonic, reconexao_s=None, sensor_tipo=None):
        self.porta = porta or obter_variavel("ARDUINO_PORTA")
        self.baudrate = baudrate
        self.sensor_tipo = sensor_tipo or obter_variavel("MAKER_SENSOR_TIPO", "analogico")
        if self.sensor_tipo not in ("analogico", "digital"):
            raise ValueError("MAKER_SENSOR_TIPO deve ser analogico ou digital.")
        self.serial = None
        self.balanco_ms = int(obter_variavel("ARDUINO_BALANCO_MS", "60"))
        self.nivel_luz_escuro = int(obter_variavel("ARDUINO_LUZ_ESCURO", "1"))
        self.reconexao_s = float(reconexao_s or obter_variavel("ARDUINO_RECONEXAO_S", "5"))
        self.sem_dados_s = float(obter_variavel("ARDUINO_SEM_DADOS_S", "10"))
        if not (1 <= self.reconexao_s <= 300 and 3 <= self.sem_dados_s <= 300):
            raise ValueError("Reconexão deve ser 1..300 s e sem dados 3..300 s.")
        self._fabrica = fabrica_serial or (serial.Serial if serial else None)
        self._relogio = relogio
        self._trava = threading.RLock()
        self._parar = threading.Event()
        self._pronto = threading.Event()
        self._thread = None
        self._buffer = bytearray()
        self._descartando = False
        self._sequencia = 0
        self._leitura = None
        self._digital = None
        self._digital_instante = None
        self._leitura_digital = None
        self.leds_separados = False
        self.sensor_luz_disponivel = False  # digital legado OU analógico
        self.sensor_analogico_disponivel = False
        self.capacidades = set()
        self.versao = 0
        self.linhas_invalidas = self.linhas_inesperadas = 0
        self._status = None
        if ativo:
            self.conectar()

    @property
    def conectado(self):
        with self._trava:
            return self.serial is not None and self.serial.is_open

    @property
    def ultima_leitura(self):
        with self._trava:
            return self._leitura

    @property
    def ultima_leitura_valida(self):
        leitura = self.ultima_leitura_sensor()
        return leitura.instante if leitura else None

    def suporta_sensor(self, tipo=None):
        tipo = tipo or self.sensor_tipo
        if tipo == "analogico":
            return self.sensor_analogico_disponivel
        return self.versao == 3 or "LUZ_DIGITAL" in self.capacidades

    def ultima_leitura_sensor(self, tipo=None):
        """Cache do sensor selecionado; DO jamais é convertido em ADC."""
        with self._trava:
            return self._leitura_digital if (tipo or self.sensor_tipo) == "digital" else self._leitura

    def obter_leitura(self, tipo=None, max_idade_s=3):
        tipo = tipo or self.sensor_tipo
        leitura = self.ultima_leitura_sensor(tipo)
        if (not self.conectado or not self.suporta_sensor(tipo) or leitura is None
                or self._relogio() - leitura.instante > max_idade_s):
            return None
        return leitura

    def leitura_atual(self, max_idade_s=3):
        return self.obter_leitura("analogico", max_idade_s)

    def _avisar(self, status):
        if status != self._status:
            print(f"[Arduino] {status}")
            self._status = status

    def conectar(self):
        """Inicia um único leitor. Aguarda o handshake sem ler a porta aqui."""
        if self._thread and self._thread.is_alive():
            return self.conectado
        self._parar.clear()
        self._pronto.clear()
        self._thread = threading.Thread(target=self._receber, name="arduino-serial", daemon=True)
        self._thread.start()
        self._pronto.wait(3.2)
        return self.conectado

    def _limpar_sessao(self):
        self.leds_separados = self.sensor_luz_disponivel = False
        self.sensor_analogico_disponivel = False
        self.capacidades = set()
        self.versao = 0
        self._leitura = self._digital = self._digital_instante = None
        self._leitura_digital = None
        self._buffer.clear()
        self._descartando = False

    def _desconectar(self):
        with self._trava:
            placa, self.serial = self.serial, None
            self._limpar_sessao()
            if placa:
                try:
                    placa.close()
                except (OSError, ValueError):
                    pass

    def _receber(self):
        """Dono exclusivo de read; conexão/reconexão também pertencem a esta tarefa."""
        while not self._parar.is_set():
            if not self.conectado:
                porta = self.porta or self.detectar_porta()
                if not porta or self._fabrica is None:
                    self._avisar("Indisponível; aguardando placa/pyserial (sem simulação).")
                    self._pronto.set()
                    self._parar.wait(self.reconexao_s)
                    continue
                try:
                    placa = self._fabrica(porta, self.baudrate, timeout=0.1, write_timeout=1)
                    with self._trava:
                        self._limpar_sessao()
                        self.serial = placa
                    abertura = self._relogio()
                    pedido_h = False
                    proxima_digital = abertura
                    self._avisar(f"Porta {porta} aberta; negociando firmware.")
                except (OSError, ValueError):
                    self._avisar("Porta indisponível; reconexão controlada.")
                    self._pronto.set()
                    self._parar.wait(self.reconexao_s)
                    continue
            try:
                # read de 1 byte + os disponíveis evita esperar preencher um bloco.
                dados = self.serial.read(max(1, min(256, self.serial.in_waiting)))
                if dados:
                    self._processar_bytes(dados)
                agora = self._relogio()
                if not pedido_h and agora - abertura >= 2:
                    self._enviar(b"H")
                    pedido_h = True
                if agora - abertura >= 3:
                    self._pronto.set()
                if self.suporta_sensor("digital") and agora >= proxima_digital:
                    self._enviar(b"L")
                    proxima_digital = agora + 1
                ultima = self.ultima_leitura_valida
                # Uma placa silenciosa também pode ter perdido comunicação.
                if agora - (ultima if ultima is not None else abertura) > self.sem_dados_s:
                    # Firmware antigo sem stream continua útil para os efeitos.
                    if self.suporta_sensor() or not self.versao:
                        raise OSError("telemetria ausente")
            except (OSError, ValueError, AttributeError):
                self._avisar("Comunicação perdida ou sem telemetria; aguardando reconexão.")
                self._desconectar()
                self._pronto.set()
                self._parar.wait(self.reconexao_s)
        self._desconectar()

    def _processar_bytes(self, dados):
        """Framing limitado: fragmentos, CRLF, excesso e bytes inválidos."""
        for byte in dados:
            if byte == 10:
                if not self._descartando:
                    self._processar_linha(bytes(self._buffer).rstrip(b"\r"))
                self._buffer.clear()
                self._descartando = False
            elif not self._descartando:
                if len(self._buffer) >= 192:
                    self._buffer.clear()
                    self._descartando = True
                    self.linhas_invalidas += 1
                else:
                    self._buffer.append(byte)

    def _processar_linha(self, linha):
        with self._trava:
            pronto = re.fullmatch(rb"PRONTO(?: v(\d+))?(?: CAPS=([A-Z0-9_,]+))?", linha)
            if pronto:
                versao = int(pronto[1] or 1)
                caps = set((pronto[2] or b"").decode("ascii").split(",")) - {""}
                self.versao, self.capacidades = versao, caps
                self.leds_separados = versao >= 2 or "LEDS_SEPARADOS" in caps
                self.sensor_analogico_disponivel = "LDR_ANALOGICO" in caps
                self.sensor_luz_disponivel = versao == 3 or self.sensor_analogico_disponivel or "LUZ_DIGITAL" in caps
                self._pronto.set()
                self._avisar(f"Firmware v{versao}; capacidades: {','.join(sorted(caps)) or 'legadas'}.")
                return
            telemetria = re.fullmatch(rb"TEL:LDR:(\d{1,4}):(\d{1,10})", linha)
            if telemetria and self.sensor_analogico_disponivel:
                bruto, uptime = int(telemetria[1]), int(telemetria[2])
                if bruto <= 1023 and uptime <= 0xFFFFFFFF:
                    self._sequencia += 1
                    self._leitura = LeituraLDR(bruto, self._relogio(), datetime.now(timezone.utc),
                                               uptime, self._sequencia)
                    return
            if linha in (b"LUZ:0", b"LUZ:1"):
                self._digital = int(linha[-1:])
                self._digital_instante = self._relogio()
                self._sequencia += 1
                self._leitura_digital = LeituraLDR(self._digital, self._digital_instante,
                    datetime.now(timezone.utc), None, self._sequencia, tipo="digital")
            elif linha.startswith((b"TEL:", b"LUZ:", b"PRONTO")):
                self.linhas_invalidas += 1
            elif linha:
                self.linhas_inesperadas += 1

    @staticmethod
    def detectar_porta():
        if list_ports is None:
            return None
        portas = list(list_ports.comports())
        for p in portas:
            if p.vid in FABRICANTES_USB:
                return p.device
        for p in portas:
            if any(nome in p.device for nome in NOMES_TIPICOS):
                return p.device
        return None

    def _enviar(self, comando):
        with self._trava:
            if not self.conectado:
                return
            try:
                self.serial.write(comando)
            except (OSError, ValueError):
                self._desconectar()
                self._avisar("Comunicação perdida ou sem telemetria; aguardando reconexão.")

    def fechar(self):
        self._parar.set()
        self.apagar_olhos()
        self.parar_cabeca()
        if self._thread:
            self._thread.join(timeout=2)
        self._desconectar()

    def ler_luminosidade(self, limite_segundos=1.5):
        """Compatibilidade: só consulta o cache, nunca lê/limpa a porta."""
        if not self.conectado or not self.sensor_luz_disponivel:
            return None
        if self.sensor_tipo == "digital":
            leitura = self.obter_leitura("digital")
            return leitura.bruto if leitura else None
        if self.sensor_analogico_disponivel:
            leitura = self.leitura_atual()
            return leitura.bruto if leitura else None
        if self._digital_instante is not None and self._relogio() - self._digital_instante <= 3:
            return self._digital
        return None

    def descrever_luminosidade(self):
        if not self.conectado:
            return "O Arduino está desconectado. A iluminação está indisponível."
        if not self.sensor_luz_disponivel:
            return "Firmware sem sensor anunciado. Carregue o sketch v4 para monitorar."
        nivel = self.ler_luminosidade()
        if nivel is None:
            return "Sem leitura recente. Confira a comunicação com o Arduino."
        if self.sensor_tipo == "analogico" and self.sensor_analogico_disponivel:
            return f"Leitura bruta do LDR em A0: {nivel} de 1023. A interpretação exige calibração."
        if self.sensor_tipo == "digital":
            return f"Leitura digital DO no pino 8: {nivel}. A interpretação exige calibração de claro e escuro."
        estado = "escuro" if nivel == self.nivel_luz_escuro else "claro"
        return f"Sensor digital legado: {nivel}, ambiente {estado}. Se o sensor usa DO em 8, configure MAKER_SENSOR_TIPO=digital."

    # ------------------------------------------------------------------ #
    #  Olhos (LEDs)
    # ------------------------------------------------------------------ #
    def acender_olhos(self, cor: str = "ambos") -> None:
        """
        Acende os LEDs dos olhos.

        cor -> "ambos" (padrão), "vermelho" (Sharingan) ou "azul" (Rinnegan).
               Com o sketch antigo na placa, qualquer cor acende os dois.
        """
        comandos = {
            "ambos": self.OLHOS_ACENDER,
            "vermelho": self.OLHO_VERMELHO,
            "azul": self.OLHO_AZUL,
        }
        if cor not in comandos:
            raise ValueError(f"Cor inválida: {cor!r}. Use 'ambos', 'vermelho' ou 'azul'.")
        if not self.leds_separados:
            cor = "ambos"
        self._enviar(comandos[cor])

    def apagar_olhos(self) -> None:
        """Apaga os dois LEDs dos olhos."""
        self._enviar(self.OLHOS_APAGAR)

    @contextmanager
    def olhos_acesos(self, cor: str = "ambos"):
        """
        Mantém os olhos acesos enquanto o bloco 'with' executa:

            with arduino.olhos_acesos("vermelho"):
                tocar_audio()

        O 'finally' garante que eles apagam mesmo se o áudio der erro.
        """
        self.acender_olhos(cor)
        try:
            yield
        finally:
            self.apagar_olhos()

    # ------------------------------------------------------------------ #
    #  Cabeça (motor DC na ponte H)
    # ------------------------------------------------------------------ #
    def girar_cabeca(self) -> None:
        """Começa a girar a cabeça. Ela continua até parar_cabeca()."""
        self._enviar(self.CABECA_GIRAR)

    def parar_cabeca(self) -> None:
        """Para a cabeça; o Arduino a leva de volta ao centro suavemente."""
        self._enviar(self.CABECA_PARAR)

    @contextmanager
    def cabeca_balancando(self, balanco_ms: int | None = None, *, duracao_max_s=None, condicao=None):
        """
        Balança a cabeça de um lado para o outro, com movimentos curtos,
        enquanto o bloco 'with' executa - sem dar voltas, para não torcer os
        fios dentro do boneco.

        balanco_ms -> quanto tempo o motor fica ligado do centro até cada
                      lado (padrão: ARDUINO_BALANCO_MS do .env, ou 60).
                      Maior = balanço mais largo.

        duracao_max_s -> limite opcional do gesto, mesmo se a fala continuar.
        condicao      -> callback opcional para interromper um aviso resolvido.

        Cada balançada é um gesto completo, como um "não, não" de cabeça:
            centro -> direita -> esquerda -> centro (pausa breve) -> de novo

        Funciona com o sketch que já está na placa, que só conhece 'G' (gira)
        e 'P' (para e volta ao centro):
            - 'G' tira a cabeça do centro para a direita;
            - "PG" seguidos viram um "inverter": o 'P' freia e marca a volta
              ao centro, e o 'G' logo depois faz o sketch seguir em frente,
              passando do centro até a esquerda;
            - um 'P' sozinho traz a cabeça de volta e a PARA no centro.
        Quem decide quando inverter é o Python, bem antes dos 600 ms em que o
        sketch inverteria sozinho - é isso que deixa o balanço curto.

        Por que parar no centro a cada gesto: cada comando chega à placa com
        alguns milissegundos de variação, e como os movimentos alternam de
        sentido, esses erros não se cancelam - vão se somando, e numa fala
        longa o balanço ia "andando" para um dos lados. Parando no centro, que
        o sketch estima, o erro de um gesto não passa para o
        próximo.
        """
        balanco = balanco_ms if balanco_ms is not None else self.balanco_ms
        # Abaixo de ~10 ms o motor nem chega a vencer a inércia; acima de 400
        # ficaria perto demais do limite em que o sketch inverte por conta.
        balanco = max(10, min(400, int(balanco)))
        if duracao_max_s is not None and (not math.isfinite(duracao_max_s) or duracao_max_s <= 0):
            raise ValueError("A duração máxima do balanço deve ser positiva e finita.")
        parar = threading.Event()

        freio, folga = PAUSA_INVERSAO_MS, 40
        # (instante em ms dentro do gesto, comando). Cada trecho começa com o
        # freio de 120 ms que o sketch faz antes de mover o motor.
        gesto = (
            (0, self.CABECA_GIRAR),                              # centro -> direita
            (freio + balanco, self.CABECA_PARAR + self.CABECA_GIRAR),  # direita -> esquerda
            (2 * freio + 3 * balanco, self.CABECA_PARAR),        # esquerda -> centro
        )
        # A cabeça chega ao centro em 3 freios + 4 balanços; a folga garante
        # que o próximo 'G' só chegue depois que ela parou.
        duracao_gesto = 3 * freio + 4 * balanco + folga

        def balancar():
            inicio = time.monotonic()
            fim = inicio + duracao_max_s if duracao_max_s is not None else float("inf")
            numero = 0
            try:
                while not parar.is_set():
                    comeco = inicio + numero * duracao_gesto / 1000
                    for instante_ms, comando in gesto:
                        alvo = comeco + instante_ms / 1000
                        while True:
                            agora = time.monotonic()
                            if parar.is_set() or agora >= fim or (condicao is not None and not condicao()):
                                return
                            espera = alvo - agora
                            if espera <= 0:
                                break
                            # Avalia cancelamento sem esperar acabar um trecho longo.
                            parar.wait(min(espera, fim - agora, .05) if condicao is not None else min(espera, fim - agora))
                        self._enviar(comando)
                    numero += 1
            finally:
                self.parar_cabeca()  # também para ao atingir limite, sem esperar a fala

        thread = threading.Thread(target=balancar, name="arduino-cabeca", daemon=True)
        thread.start()
        try:
            yield
        finally:
            parar.set()
            thread.join()
