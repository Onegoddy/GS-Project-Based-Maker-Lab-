"""
Módulo de VOZ da assistente M.A.D.A.R.A.

Responsável por:
    - Reconhecer comandos de voz (Speech-to-Text) usando a API do Google
      através da biblioteca 'speech_recognition'.
    - Sintetizar respostas em áudio (Text-to-Speech). Suporta dois motores:
        * 'edge'    -> voz de PERSONAGEM (grave/sombria, estilo Madara Uchiha)
                       usando o edge-tts (gratuito, sem chave de API).
        * 'pyttsx3' -> voz padrão do sistema, 100% offline (fallback).

Baseado nos códigos do professor:
    - reconhecedor.py  (speech_recognition + recognize_google)
    - sintetizador1.py (pyttsx3)
    - sintetizador2.py (gTTS + pygame -> reaproveitado aqui para tocar o mp3)

Como funciona a voz "assustadora":
    O edge-tts permite ajustar o TOM (pitch) e a VELOCIDADE (rate) da fala.
    Usamos uma voz masculina em português e abaixamos o tom para dar aquele
    ar grave e ameaçador. Ajuste os valores em EDGE_PITCH / EDGE_RATE (abaixo
    ou no arquivo .env) para deixar do seu gosto.
"""

import os
import asyncio
import importlib.util
import tempfile
import time
import queue
import threading
import math
from contextlib import ExitStack, contextmanager

from .config import obter_variavel


class Voz:
    """Encapsula tudo que envolve ouvir e falar da assistente."""

    def __init__(
        self,
        idioma: str = "pt-BR",
        taxa_fala: int = 190,
        motor: str = "auto",
        arduino=None,
        escutar: bool = True,
    ):
        """
        Parâmetros:
            idioma    -> idioma usado no reconhecimento e na síntese.
            taxa_fala -> velocidade da fala (só usada no motor pyttsx3).
            motor     -> 'auto' (tenta a voz de personagem e cai no pyttsx3 se
                         faltar internet/biblioteca), 'edge' ou 'pyttsx3'.
            arduino   -> objeto Arduino (opcional). Se informado, os olhos do
                         boneco acendem durante cada fala.
            escutar   -> False para apenas emitir alertas, sem carregar
                         SpeechRecognition ou acessar o microfone.
        """
        self.idioma = idioma
        self.taxa_fala = taxa_fala
        self.arduino = arduino

        self.reconhecedor = None
        self._sr = None
        self._ruido_calibrado = False
        self._microfone_contexto = None
        self._microfone = None
        self._estado_escuta = None
        if escutar:
            self._iniciar_reconhecimento()

        # ----- Configuração da voz de personagem (edge-tts) -----
        self.edge_voz = obter_variavel("EDGE_VOICE", "pt-BR-AntonioNeural")
        self.edge_pitch = obter_variavel("EDGE_PITCH", "-20Hz")
        self.edge_rate = obter_variavel("EDGE_RATE", "+10%")
        escolhido = motor if motor != "auto" else obter_variavel("VOZ_MOTOR", "auto")
        if escolhido not in ("auto", "edge", "pyttsx3"):
            raise ValueError("VOZ_MOTOR deve ser auto, edge ou pyttsx3.")
        self.motor = "edge" if escolhido == "auto" else escolhido
        self._edge_intervalo_s = float(obter_variavel("VOZ_EDGE_RETRY_S", "300"))
        if not math.isfinite(self._edge_intervalo_s) or not 0 < self._edge_intervalo_s <= 3600:
            raise ValueError("VOZ_EDGE_RETRY_S deve estar entre 0 e 3600 segundos.")
        self._edge_tentar_apos = float("-inf")

        # Voz em português usada no fallback pyttsx3.
        self.voz_id = self._descobrir_voz_portugues()
        self._fila = queue.Queue(maxsize=64)
        self._audio_lock = threading.Lock()
        self._dialogo_lock = threading.Lock()
        self._dialogos_ativos = 0
        self._falando = threading.Event()
        self._parar = threading.Event()
        self._fechada = False
        self._fim_fala = float("-inf")
        self._motor_ativo = None
        self._thread = threading.Thread(target=self._reproduzir_fila, name="voz-saida", daemon=True)
        self._thread.start()

    @property
    def em_dialogo(self):
        with self._dialogo_lock:
            return self._dialogos_ativos > 0

    @contextmanager
    def dialogo(self):
        """Reserva as próximas janelas de escuta; a coleta/avaliação nunca dependem disso."""
        with self._dialogo_lock:
            self._dialogos_ativos += 1
        try:
            yield
        finally:
            with self._dialogo_lock:
                self._dialogos_ativos -= 1

    @staticmethod
    def verificar_sintese() -> None:
        """Falha com orientação clara se nenhum motor de áudio está instalado."""
        tem = lambda nome: importlib.util.find_spec(nome) is not None
        if not tem("pyttsx3") and not (tem("edge_tts") and tem("pygame")):
            raise RuntimeError(
                "Faltam as dependências para falar. Execute: "
                "python -m pip install -r requirements-maker.txt"
            )

    def _iniciar_reconhecimento(self) -> None:
        """Carrega SpeechRecognition somente para os caminhos que escutam."""
        if self.reconhecedor is not None:
            return
        try:
            import speech_recognition as sr
        except ImportError as erro:
            raise RuntimeError(
                "Para ouvir comandos, instale SpeechRecognition e PyAudio: "
                "python -m pip install SpeechRecognition PyAudio"
            ) from erro
        self._sr = sr
        self.reconhecedor = sr.Recognizer()
        self.reconhecedor.operation_timeout = 5

        # ----- Ajustes para ele não cortar a frase no meio -----
        # pause_threshold: quanto tempo de SILÊNCIO encerra a frase. O padrão
        # da biblioteca é 0.8s, curto demais para frases como "cadastrar
        # evento reunião com o grupo na terça" - qualquer pausa para respirar
        # já era interpretada como fim da fala.
        self.reconhecedor.pause_threshold = 1.8

        # non_speaking_duration precisa ser <= pause_threshold, senão a
        # biblioteca levanta erro.
        self.reconhecedor.non_speaking_duration = 0.6

        # dynamic_energy_threshold: com True (padrão) o limiar de "o que é
        # voz" é recalculado DURANTE a fala. Numa pausa curta ele sobe, e o
        # resto da sua frase passa a ser tratado como ruído de fundo. É a
        # principal causa do corte precoce - desligamos e calibramos uma vez.
        self.reconhecedor.dynamic_energy_threshold = False

    # ------------------------------------------------------------------ #
    #  Text-to-Speech (falar) - ponto único de entrada
    # ------------------------------------------------------------------ #
    def falar(self, texto: str, efeito=None, olhos: str = "ambos") -> None:
        """
        Fala um texto em voz alta (e também imprime no terminal).

        efeito -> opcional: função que devolve um bloco 'with' para rodar
                  ENQUANTO o áudio toca, junto com os olhos. Exemplo:
                      voz.falar(frase, efeito=arduino.cabeca_balancando)
                  Passamos a FUNÇÃO (sem parênteses), e não o bloco já
                  criado, porque ele pode ser usado duas vezes: se a voz de
                  personagem falhar no meio, o pyttsx3 toca a fala de novo.
        olhos  -> quais LEDs acendem durante a fala: "ambos" (padrão),
                  "vermelho" (Sharingan) ou "azul" (Rinnegan).
        """
        terminou = self.enfileirar(texto, efeito=efeito, olhos=olhos)
        terminou.wait()

    def enfileirar(self, texto, efeito=None, olhos="ambos", condicao=None):
        """Todas as falas usam esta fila. Alertas podem ser cancelados se já resolvidos."""
        terminou = threading.Event()
        if self._fechada:
            terminou.set()
            return terminou
        try:
            self._fila.put_nowait((texto, efeito, olhos, condicao, terminou))
        except queue.Full:
            print("[Voz] Fila cheia; aviso permanece disponível no diagnóstico.")
            terminou.set()
        return terminou

    def _reproduzir_fila(self):
        while not self._parar.is_set():
            try:
                texto, efeito, olhos, condicao, terminou = self._fila.get(timeout=0.1)
            except queue.Empty:
                continue
            self._falando.set()
            try:
                # Microfone e reprodução nunca ficam ativos juntos.
                with self._audio_lock:
                    if not self._parar.is_set() and (condicao is None or condicao()):
                        # A sessão fica aberta durante silêncio; só é liberada
                        # ao processar uma frase, falar, falhar ou encerrar.
                        self._liberar_microfone()
                        self._falar_agora(texto, efeito, olhos, condicao=condicao)
                    self._fim_fala = time.monotonic()
            except Exception as erro:
                print(f"[Voz] Falha na saída: {type(erro).__name__}.")
            finally:
                self._falando.clear()
                terminou.set()
                self._fila.task_done()

    def fechar(self):
        self._fechada = True
        self._parar.set()
        if self._motor_ativo is not None:
            try:
                self._motor_ativo.stop()
            except RuntimeError:
                print("[Voz] O motor de fala não respondeu à interrupção.")
        while True:
            try:
                item = self._fila.get_nowait()
                item[-1].set()
                self._fila.task_done()
            except queue.Empty:
                break
        self._thread.join(timeout=20)
        if self._thread.is_alive():
            raise RuntimeError("A saída de voz não encerrou no prazo. Confira o dispositivo de áudio.")
        with self._audio_lock:
            self._liberar_microfone()

    def _falar_agora(self, texto, efeito=None, olhos="ambos", condicao=None):
        print(f"MADARA: {texto}")
        # Tenta primeiro a voz de personagem (edge-tts); se falhar, usa pyttsx3.
        if self.motor == "edge" and time.monotonic() >= self._edge_tentar_apos:
            sucesso = (self._falar_edge(texto, efeito, olhos, condicao=condicao) if condicao
                       else self._falar_edge(texto, efeito, olhos))
            if sucesso:
                return
            self._edge_tentar_apos = time.monotonic() + self._edge_intervalo_s
        if self._parar.is_set() or (condicao is not None and not condicao()):
            print("[Voz] Aviso cancelado: condição encerrada antes da reprodução.")
            return
        self._falar_pyttsx3(texto, efeito, olhos)

    # ------------------------------------------------------------------ #
    #  Motor 1: edge-tts (voz de personagem, grave/sombria)
    # ------------------------------------------------------------------ #
    def _falar_edge(self, texto: str, efeito=None, olhos: str = "ambos", condicao=None) -> bool:
        """Gera o áudio com edge-tts e o reproduz. Retorna True se der certo."""
        try:
            import edge_tts

            # Salva num caminho temporário SEM acento (evita problemas no Windows).
            arquivo, caminho = tempfile.mkstemp(prefix="madara_", suffix=".mp3")
            os.close(arquivo)

            async def _gerar():
                comunicador = edge_tts.Communicate(
                    texto,
                    voice=self.edge_voz,
                    rate=self.edge_rate,
                    pitch=self.edge_pitch,
                )
                await asyncio.wait_for(comunicador.save(caminho), timeout=15)

            asyncio.run(_gerar())  # executa a rotina assíncrona do edge-tts
            if self._parar.is_set() or (condicao is not None and not condicao()):
                print("[Voz] Aviso cancelado: condição encerrada durante a síntese.")
                return True
            with self._durante_o_audio(efeito, olhos):
                self._tocar_mp3(caminho)
            return True
        except Exception as erro:
            print(f"[Voz] Falha na voz de personagem ({type(erro).__name__}). "
                  f"Usando a voz padrão; nova tentativa online após {self._edge_intervalo_s:g} s.")
            return False
        finally:
            if "caminho" in locals():
                try:
                    os.unlink(caminho)
                except OSError:
                    pass

    @contextmanager
    def _durante_o_audio(self, efeito=None, olhos: str = "ambos"):
        """
        Liga os olhos do boneco (e o 'efeito' extra, se houver) durante o
        bloco 'with', e desliga tudo no fim - mesmo se o áudio der erro.

        Usado só em volta da REPRODUÇÃO do áudio, não da fala inteira: o
        edge-tts leva cerca de 1 segundo gerando o mp3 pela internet antes
        de tocar, e qualquer efeito ligado antes disso ficaria fora de
        sincronia com a voz. Sem Arduino, só o 'efeito' (se houver) roda.
        """
        with ExitStack() as pilha:
            if self.arduino is not None:
                pilha.enter_context(self.arduino.olhos_acesos(olhos))
            if efeito is not None:
                pilha.enter_context(efeito())
            yield

    def _tocar_mp3(self, caminho: str) -> None:
        """Reproduz um arquivo mp3 usando o mixer do pygame."""
        from pygame import mixer

        if not mixer.get_init():
            mixer.init()
        mixer.music.load(caminho)
        mixer.music.play()
        while mixer.music.get_busy() and not self._parar.is_set():
            # Espera em pausas de 10 ms em vez de um laço vazio: o laço vazio
            # ocupava 100% do processador durante a fala inteira e atrasava a
            # thread que balança a cabeça do boneco no mesmo momento.
            time.sleep(0.01)
        mixer.music.stop()
        mixer.music.unload()

    # ------------------------------------------------------------------ #
    #  Motor 2: pyttsx3 (voz padrão, offline) - fallback
    # ------------------------------------------------------------------ #
    def _descobrir_voz_portugues(self):
        """Retorna o id de uma voz em português instalada no sistema (ou None)."""
        try:
            import pyttsx3
            motor = pyttsx3.init()
            escolhido = None
            for voz in motor.getProperty("voices"):
                nome = (voz.name or "").lower()
                if "portug" in nome or "brazil" in nome or "brasil" in nome:
                    escolhido = voz.id
                    break
            motor.stop()
            return escolhido
        except Exception as erro:
            print(f"[Voz] Não foi possível listar as vozes: {erro}")
            return None

    def _falar_pyttsx3(self, texto: str, efeito=None, olhos: str = "ambos") -> None:
        """
        Fala usando o pyttsx3. Recria o motor a cada chamada para evitar o bug
        do 'fala só na primeira vez' no Windows.
        """
        try:
            import pyttsx3
            motor = pyttsx3.init()
            self._motor_ativo = motor
            motor.setProperty("volume", 1.0)
            motor.setProperty("rate", self.taxa_fala)
            if self.voz_id:
                motor.setProperty("voice", self.voz_id)
            motor.say(texto)
            with self._durante_o_audio(efeito, olhos):
                motor.runAndWait()  # é aqui que o áudio realmente toca
            motor.stop()  # encerra o motor para a próxima fala funcionar
        except Exception as erro:
            print(f"[Voz] Falha ao sintetizar áudio: {erro}")
        finally:
            self._motor_ativo = None

    # ------------------------------------------------------------------ #
    #  Speech-to-Text (ouvir)
    # ------------------------------------------------------------------ #
    def ouvir(
        self,
        ajustar_ruido: bool = True,
        timeout: int | None = 1,
        limite_frase: int | None = 15,
    ) -> str:
        """
        Captura o áudio do microfone e o converte em texto.

        Parâmetros:
            ajustar_ruido -> calibra o ruído do ambiente (só na 1a chamada).
            timeout       -> segundos esperando você COMEÇAR a falar.
            limite_frase  -> duração máxima da frase, em segundos. Impede que
                             um ruído contínuo prenda a escuta para sempre.

        Retorna a frase reconhecida em minúsculas, ou uma string vazia
        se nada for entendido.
        """
        if self._fechada:
            return ""
        self._iniciar_reconhecimento()
        if self._falando.is_set() or not self._fila.empty():
            self._parar.wait(0.1)
            return ""
        with self._audio_lock:
            if self._falando.is_set() or self._fechada:
                return ""
            restante = 0.3 - (time.monotonic() - self._fim_fala)
            if restante > 0:
                self._parar.wait(restante)
            if self._fechada or self._parar.is_set() or self._falando.is_set():
                return ""
            return self._ouvir_sem_reproducao(ajustar_ruido, timeout or 1, limite_frase or 15)

    def _obter_microfone(self):
        """Chamado sob _audio_lock; reutiliza a sessão entre timeouts de silêncio."""
        if self._microfone is None:
            try:
                contexto = self._sr.Microphone()
                microfone = contexto.__enter__()
            except (OSError, AttributeError, ImportError) as erro:
                raise RuntimeError("Não foi possível abrir o microfone. Confira o dispositivo, permissões e requirements-microfone.txt.") from erro
            if getattr(microfone, "stream", None) is None:
                # SpeechRecognition termina o PortAudio se open() falha e pode
                # retornar um objeto sem stream. Não usa uma sessão inválida.
                raise RuntimeError("O microfone não abriu um fluxo de áudio. Confira o dispositivo selecionado.")
            self._microfone_contexto, self._microfone = contexto, microfone
        return self._microfone

    def _liberar_microfone(self):
        """Chamado sob _audio_lock, sem captura/reprodução simultâneas."""
        contexto = self._microfone_contexto
        self._microfone_contexto = self._microfone = None
        self._estado_escuta = None
        if contexto is not None:
            try:
                contexto.__exit__(None, None, None)
            except Exception as erro:
                print(f"[Voz] Falha ao liberar o microfone: {type(erro).__name__}.")

    def _ouvir_sem_reproducao(self, ajustar_ruido, timeout, limite_frase):
        sr = self._sr
        try:
            microfone = self._obter_microfone()
            # Calibra UMA única vez. Recalibrar a cada escuta (como antes)
            # somava 1s de espera toda vez e ainda podia elevar o limiar de
            # ruído a ponto de a assistente parar de ouvir vozes mais baixas.
            if ajustar_ruido and not self._ruido_calibrado:
                print("🔧 Calibrando o microfone... (fique em silêncio 1 segundo)")
                self.reconhecedor.adjust_for_ambient_noise(microfone, duration=1)
                self._ruido_calibrado = True

            if self._estado_escuta != "ouvindo":
                print("🎤 Ouvindo... (aguardando comando)")
                self._estado_escuta = "ouvindo"
            try:
                audio = self.reconhecedor.listen(
                    microfone, timeout=timeout, phrase_time_limit=limite_frase
                )
            except sr.WaitTimeoutError:
                if self._parar.is_set():
                    self._liberar_microfone()
                return ""
        except BaseException:
            self._ruido_calibrado = False
            self._liberar_microfone()
            raise
        # Descarta a sessão antes da consulta/transcrição. Assim não acumula
        # áudio antigo enquanto a assistente interpreta ou responde ao comando.
        self._liberar_microfone()
        if self._parar.is_set():
            return ""
        # Envia o áudio para a API do Google converter em texto.
        try:
            texto = self.reconhecedor.recognize_google(audio, language=self.idioma)
            print(f"Você disse: {texto}")
            return texto.lower().strip()
        except sr.UnknownValueError:
            print("[Voz] Não entendi o que foi dito.")
            return ""
        except sr.RequestError as erro:
            print(f"[Voz] Erro ao acessar o serviço de reconhecimento: {erro}")
            return ""
        except OSError as erro:
            # O áudio é convertido para FLAC antes de ir ao Google. Sem um
            # 'flac' instalado, a biblioteca usa uma cópia embutida que só
            # roda em processador Intel - num Mac com chip Apple e sem
            # Rosetta, isso derrubava a assistente inteira.
            print(f"[Voz] Não consegui preparar o áudio ({erro}). "
                  "No macOS, instale o conversor com: brew install flac")
            return ""
