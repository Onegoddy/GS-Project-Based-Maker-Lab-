"""Intenções locais antes dos gatilhos genéricos de hora, clima e cálculo."""
import re
import unicodedata


def normalizar(texto):
    texto = unicodedata.normalize("NFKD", texto.lower())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(re.findall(r"\w+", texto))


def intencao_energia(texto):
    t = normalizar(texto)
    if (re.search(r"\b(?:abre|abertura|abrimos)\b.+\b(?:fecha|fechamento|fechamos)\b", t)
            or (re.search(r"\b(configurar|configurar o|definir|alterar|mudar|ajustar)\b", t)
                and re.search(r"expediente|horario de funcionamento|horarios de funcionamento", t))):
        return "configurar_expediente"
    if re.search(r"expediente|horario(?:s)? de funcionamento|horario de abertura|horario de fechamento|"
                 r"que horas (?:a loja )?(?:abre|fecha)|quando (?:a loja )?(?:abre|fecha)", t):
        return "expediente"
    if any(p in t for p in ("pausar monitoramento", "pause o monitoramento", "pausar o monitoramento")):
        return "pausar"
    if any(p in t for p in ("retomar monitoramento", "retome o monitoramento", "retomar o monitoramento",
                            "monitorar energia", "iniciar monitoramento de energia", "ativar modo economia")):
        return "retomar"
    if "resumo de energia" in t or "resumo energia" in t:
        return "resumo"
    if (re.search(r"\b(custo|custou|gasto|gastou|reais)\b", t)
            and re.search(r"\b(energia|iluminacao|luz|ocorrencia|alerta)\b", t)):
        return "custo"
    if "ultimo alerta" in t or re.search(r"(?:explique|explicar|por que).*(?:alerta|ocorrencia)", t):
        return "ultimo_alerta"
    if re.search(r"(?:qual|quanto|configurad|cadastrad).*(?:tarifa|potencia)|(?:tarifa|potencia).*(?:configurad|cadastrad)|bandeira atual|tarifa atual", t):
        return "parametros"
    if re.search(r"\b(?:quanto|qual|quantos).*(?:consumo|kwh|quilowatt)", t):
        return "consumo"
    if (re.search(r"\b(energia|iluminacao|luminosidade|luz|luzes|ldr|estoque|ambiente|ocorrencia)\b", t)
            and re.search(r"como esta|como estao|qual.*estado|status|quanto tempo|quantas horas|ha quanto|ler |testar |devo desligar|devo apagar", t)):
        return "consulta"
    if re.search(r"\b(energia|consumo|potencia|economizar|economia|eficiencia|reduzir|dicas|tarifas?|bandeiras?|eletric[ao]s?|"
                 r"eletricidade|disjuntor|fiacao|tomadas?|circuitos?|fios?|quadro eletrico|fase|neutro|aterramento|chuveiro|lampadas?|led|refrigeracao|geladeira|freezer|solar)\b|"
                 r"ar condicionado|consultor de energia", t):
        return "conversar"
    if re.search(r"\b(energia|iluminacao|luminosidade|luz|luzes|ldr)\b", t):
        return "consulta"
    return None


def encerrar_assistente(texto):
    return normalizar(texto) in {"encerrar assistente", "encerrar a assistente", "encerrar programa",
                                "encerrar o programa", "desligar assistente", "desligar a assistente",
                                "tchau", "adeus"}
