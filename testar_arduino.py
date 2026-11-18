"""
Script AUXILIAR - Teste do hardware do boneco (Arduino).

Testa os olhos e o BALANÇO da cabeça SEM precisar falar com a assistente.
A cabeça balança de um lado para o outro, sem dar voltas, para não torcer
os fios dentro do boneco.

Como usar:
    1) A placa precisa estar com o sketch 'arduino/madara_arduino'.
    2) Feche o Monitor Serial do Arduino IDE (só um programa pode usar a
       porta por vez).
    3) Rode um dos dois:

        python testar_arduino.py        -> começa com um balanço pequeno e
                                           vai aumentando até vocês gostarem
        python testar_arduino.py 80     -> balança direto com 80 ms
        python testar_arduino.py --sensor -> acompanha DO em 8 ou ADC em A0
        python testar_arduino.py --calibrar -> imprime referências medidas

O número é quanto tempo o motor fica ligado do centro até cada lado: maior
= balanço mais largo. Quando acharem o valor certo, grave no .env:

    ARDUINO_BALANCO_MS=80

e o "madara hashirama" passa a usar esse balanço.
"""

import sys
import time
from statistics import median

from modulos.arduino import FABRICANTES_USB, Arduino, list_ports

BALANCO_INICIAL = 30   # ms - começa pequeno: a cabeça não está bem fixada
PASSO = 15             # ms a mais (ou a menos) a cada tentativa
BALANCO_MAXIMO = 300   # ms - acima disso já seria um giro, não um balanço
TEMPO_BALANCANDO = 3   # segundos de balanço em cada tentativa


def listar_portas() -> None:
    """Mostra as portas seriais, para ajudar se a placa não for encontrada."""
    if list_ports is None:
        return
    print("Portas seriais encontradas:")
    for p in list_ports.comports():
        fabricante = FABRICANTES_USB.get(p.vid, "-")
        print(f"  {p.device:<32} {fabricante:<14} {p.description}")
    print()


def balancar(arduino: Arduino, balanco_ms: int, segundos: float) -> None:
    """Balança a cabeça com os olhos acesos, como na fala do MADARA."""
    with arduino.olhos_acesos(), arduino.cabeca_balancando(balanco_ms):
        time.sleep(segundos)
    time.sleep(1)  # tempo para a cabeça voltar ao centro


def ajustar_balanco(arduino: Arduino) -> int | None:
    """Aumenta o balanço aos poucos até vocês gostarem. None = desistiram."""
    print("Vamos ajustar o tamanho do balanço da cabeça.")
    print("Depois de cada tentativa, responda:")
    print("  s     = está bom assim")
    print("  Enter = balançar MAIS")
    print("  m     = balançar MENOS")
    print("  q     = sair\n")

    balanco = BALANCO_INICIAL
    while True:
        print(f"  Balanço de {balanco} ms...", end=" ", flush=True)
        balancar(arduino, balanco, TEMPO_BALANCANDO)
        resposta = input("e aí? [s / Enter / m / q] ").strip().lower()

        if resposta.startswith("s"):
            return balanco
        if resposta.startswith("q"):
            return None
        if resposta.startswith("m"):
            balanco = max(BALANCO_INICIAL, balanco - PASSO)
        elif balanco + PASSO > BALANCO_MAXIMO:
            print(f"  (já está no máximo de {BALANCO_MAXIMO} ms para um balanço)")
        else:
            balanco += PASSO


def testar_sensor(arduino: Arduino, segundos: float = 20) -> None:
    """Mostra somente a entrada configurada, sem inferir integridade do sensor."""
    tipo = arduino.sensor_tipo
    if not arduino.suporta_sensor():
        raise SystemExit(f"Firmware sem suporte ao modo {tipo}. Confira MAKER_SENSOR_TIPO e as capacidades.")
    rotulo = "DO (pino 8)" if tipo == "digital" else "ADC (A0)"
    print(f"Modo {tipo}: {rotulo}. Cubra e ilumine o sensor.")
    if tipo == "digital":
        print("DO deve mudar entre 0 e 1. Se houver trimpot, ajuste o limiar do módulo até distinguir as condições.")
    else:
        print("ADC 0..1023. Precisa de AO em A0 ou LDR com divisor; DO em 8 não alimenta A0.")
    fim = time.monotonic() + segundos
    niveis = set()
    while time.monotonic() < fim:
        leitura = arduino.obter_leitura()
        if leitura is None:
            print(f"Leitura indisponível | Arduino={arduino.conectado}")
        else:
            niveis.add(leitura.bruto)
            print(f"{rotulo}={leitura.bruto} | idade={time.monotonic()-leitura.instante:.2f}s | Arduino={arduino.conectado}", flush=True)
        time.sleep(0.5)
    print(f"Valores diferentes observados: {len(niveis)}. Isso não comprova integridade elétrica do LDR.")


def coletar_amostras(arduino, segundos=5, relogio=None, esperar=None):
    relogio, esperar = relogio or time.monotonic, esperar or time.sleep
    inicio = relogio()
    valores, sequencia = [], None
    while relogio() < inicio + segundos:
        leitura = arduino.obter_leitura()
        if leitura and leitura.instante >= inicio and leitura.sequencia != sequencia:
            valores.append(leitura.bruto)
            sequencia = leitura.sequencia
        esperar(0.1)
    return valores


def calibrar_sensor(arduino):
    tipo = arduino.sensor_tipo
    if not arduino.suporta_sensor():
        raise SystemExit(f"Calibração exige suporte ao modo {tipo} anunciado pelo firmware.")
    print(f"Calibração do modo {tipo}: " + ("DO no pino 8, níveis reais 0/1." if tipo == "digital" else "ADC em A0."))
    referencias = []
    for condicao in ("escuro", "iluminado"):
        input(f"Prepare o estoque {condicao}, sem interferência dos olhos/cabeça. Pressione Enter para coletar 5 s: ")
        valores = coletar_amostras(arduino)
        if len(valores) < 3:
            raise SystemExit("Poucas leituras válidas. Confira firmware, entrada selecionada e comunicação.")
        if tipo == "digital" and (set(valores) - {0, 1} or len(set(valores)) != 1):
            raise SystemExit(f"DO instável ou inválido no ambiente {condicao}. Ajuste o limiar do módulo, se houver, e repita.")
        referencias.append(int(median(valores)))
        print(f"{condicao}: mediana={referencias[-1]}, mínimo={min(valores)}, máximo={max(valores)}")
    if tipo == "digital" and referencias[0] == referencias[1]:
        raise SystemExit("DO ficou igual no escuro e no iluminado. Confira DO→8 e ajuste o trimpot, se houver, até obter 0 e 1 distintos. Nenhuma referência foi aceita.")
    if tipo == "analogico" and abs(referencias[0] - referencias[1]) < 20:
        raise SystemExit("ADC sem contraste suficiente. Confira AO→A0 ou divisor resistivo; DO→8 exige MAKER_SENSOR_TIPO=digital. Nenhuma referência foi aceita.")
    print("Copie para o .env (valores medidos nesta montagem):")
    print(f"MAKER_SENSOR_TIPO={tipo}\nMAKER_REFERENCIA_ESCURO={referencias[0]}\nMAKER_REFERENCIA_CLARO={referencias[1]}\nMAKER_SENTIDO=auto")
    return tuple(referencias)


def main() -> None:
    listar_portas()
    arduino = Arduino()
    if not arduino.conectado:
        raise SystemExit(
            "Sem Arduino conectado. Confira o cabo, ou defina ARDUINO_PORTA no "
            ".env com uma das portas listadas acima."
        )

    # O 'finally' para a cabeça e apaga os olhos mesmo com Ctrl+C.
    try:
        if sys.argv[1:] == ["--calibrar"]:
            calibrar_sensor(arduino)
            return
        if sys.argv[1:] == ["--sensor"]:
            testar_sensor(arduino)
            return

        # Cada cor separada confere a fiação: o vermelho tem que estar no
        # pino 9 e o azul no pino 10.
        for cor, descricao in (("vermelho", "só o VERMELHO (Sharingan, pino 9)"),
                               ("azul", "só o AZUL (Rinnegan, pino 10)"),
                               ("ambos", "os DOIS juntos")):
            print(f"Acendendo {descricao}...")
            arduino.acender_olhos(cor)
            time.sleep(1.2)
            arduino.apagar_olhos()
            time.sleep(0.5)
        print()

        if len(sys.argv) > 1:
            balanco = int(sys.argv[1])
            print(f"Balançando a cabeça por 5 segundos com {balanco} ms...")
            balancar(arduino, balanco, 5)
        else:
            balanco = ajustar_balanco(arduino)

        if balanco is not None:
            print(f"\nBalanço escolhido: {balanco} ms. Para usar no projeto, "
                  "grave no .env:")
            print(f"    ARDUINO_BALANCO_MS={balanco}")
    finally:
        arduino.fechar()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nTeste encerrado.")
