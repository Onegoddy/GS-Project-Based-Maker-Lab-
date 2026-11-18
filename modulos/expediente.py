"""Horários em português e persistência exclusiva do expediente, sem editar .env."""
from dataclasses import dataclass
from datetime import datetime
import re
import unicodedata


class HorarioInvalido(ValueError):
    pass


class HorarioAmbiguo(HorarioInvalido):
    pass


def texto_normalizado(texto):
    texto = unicodedata.normalize("NFKD", texto.lower())
    return " ".join("".join(c for c in texto if not unicodedata.combining(c)).split())


def _numero(texto):
    unidades = {"zero": 0, "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3,
                "quatro": 4, "cinco": 5, "seis": 6, "sete": 7, "oito": 8, "nove": 9,
                "dez": 10, "onze": 11, "doze": 12, "treze": 13, "quatorze": 14,
                "catorze": 14, "quinze": 15, "dezesseis": 16, "dezassete": 17,
                "dezessete": 17, "dezoito": 18, "dezenove": 19}
    dezenas = {"vinte": 20, "trinta": 30, "quarenta": 40, "cinquenta": 50}
    if re.fullmatch(r"\d{1,2}", texto):
        return int(texto)
    if texto in unidades:
        return unidades[texto]
    partes = texto.split(" e ")
    if partes[0] in dezenas:
        if len(partes) == 1:
            return dezenas[partes[0]]
        if len(partes) == 2 and partes[1] in unidades and 1 <= unidades[partes[1]] <= 9:
            return dezenas[partes[0]] + unidades[partes[1]]
    return None


def interpretar_horario(texto):
    """Retorna HH:MM. Horas curtas sem período são deliberadamente ambíguas."""
    t = texto_normalizado(texto).strip(" .,!?")
    t = re.sub(r"^(?:(?:madara|as|a|abre|fecha|abertura|fechamento)\s+)+", "", t)
    t = re.sub(r"\s+por favor$", "", t)
    if t in ("meia noite", "meia-noite"):
        return "00:00"
    if t in ("meio dia", "meio-dia"):
        return "12:00"
    periodo = None
    m = re.search(r"\s+(?:da|de|a)\s+(manha|tarde|noite|madrugada)$", t)
    if m:
        periodo, t = m[1], t[:m.start()].strip()
    t = re.sub(r"\b(?:horas?|minutos?)\b", " ", t)
    t = " ".join(t.split())
    numerico = re.fullmatch(r"(\d{1,2})\s*(?::\s*(\d{2})|h\s*(\d{2})?)", t)
    if numerico:
        minutos = numerico[2] or numerico[3]
        h, minuto = int(numerico[1]), int(minutos or 0)
        explicito = minutos is not None  # 07:00 é notação de 24 horas
    else:
        explicito, minuto = False, 0
        h = _numero(t)
        if h is None:
            for sufixo, valor in ((" e meia", 30), (" e um quarto", 15)):
                if t.endswith(sufixo):
                    h, minuto = _numero(t[:-len(sufixo)]), valor
                    break
        if h is None:
            candidatos = []
            for separador in re.finditer(r" e ", t):
                hora, minutos = _numero(t[:separador.start()]), _numero(t[separador.end():])
                if hora is not None and minutos is not None and 0 <= hora <= 23 and 0 <= minutos <= 59:
                    candidatos.append((hora, minutos))
            if len(candidatos) == 1:
                h, minuto = candidatos[0]
    if h is None or not 0 <= h <= 23 or not 0 <= minuto <= 59:
        raise HorarioInvalido("Não reconheci um horário válido. Diga, por exemplo, sete da manhã ou 21:00.")
    if periodo is None and not explicito and 1 <= h <= 12:
        raise HorarioAmbiguo("Informe também manhã, tarde, noite ou madrugada; ou diga o horário completo como 07:00.")
    hora_original = h
    if periodo in ("manha", "madrugada"):
        if h == 12 and periodo == "madrugada":
            h = 0
        elif h > 11 or (periodo == "madrugada" and h > 5):
            raise HorarioInvalido("O horário não combina com o período. Esclareça usando HH:MM.")
    elif periodo == "tarde":
        if 1 <= h <= 11:
            h += 12
        elif not 12 <= h <= 18:
            raise HorarioInvalido("O horário não combina com a tarde. Esclareça usando HH:MM.")
    elif periodo == "noite":
        if 6 <= h <= 11:
            h += 12
        elif h == 12:
            h = 0
        elif 13 <= h <= 17:
            raise HorarioInvalido("O horário não combina com a noite. Esclareça usando HH:MM.")
    if explicito and periodo and h != hora_original:
        raise HorarioInvalido("O período contradiz o horário de vinte e quatro horas. Esclareça usando HH:MM.")
    return f"{h:02d}:{minuto:02d}"


def extrair_horarios_frase(texto):
    t = texto_normalizado(texto)
    m = re.search(r"\b(?:abre|abertura|abrimos)\s+(?:as\s+)?(.+?)\s+(?:e\s+)?"
                  r"(?:a loja\s+)?(?:fecha|fechamento|fechamos)\s+(?:as\s+)?(.+)$", t)
    return (m[1], m[2].strip(" .!?")) if m else None


@dataclass(frozen=True)
class Expediente:
    abertura: str
    fechamento: str

    def __post_init__(self):
        for horario in (self.abertura, self.fechamento):
            if not isinstance(horario, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", horario):
                raise HorarioInvalido("Use horários locais no formato HH:MM.")
        if self.abertura == self.fechamento:
            raise HorarioInvalido("Abertura e fechamento não podem ser iguais; informe o expediente novamente.")

    @property
    def atravessa_meia_noite(self):
        return self.abertura > self.fechamento


class ArquivoExpediente:
    """Arquivo por loja/equipamento na mesma pasta do monitor; não contém segredos."""
    def __init__(self, registro):
        self.registro = registro

    def carregar(self, config):
        dados = self.registro.ler_documento("expediente.json")
        if dados is None:
            return None
        campos = {"versao", "loja_id", "equipamento_id", "abertura", "fechamento", "atualizado_em", "fuso"}
        if (not isinstance(dados, dict) or set(dados) != campos or dados["versao"] != 1
                or dados["fuso"] != "-03:00" or dados["loja_id"] != config.loja_id
                or dados["equipamento_id"] != config.equipamento_id):
            raise ValueError("expediente.json inválido ou pertence a outra loja/equipamento; revise o arquivo.")
        try:
            instante = datetime.fromisoformat(dados["atualizado_em"])
            if instante.utcoffset() is None:
                raise ValueError("Fuso ausente")
        except (ValueError, TypeError):
            raise ValueError("expediente.json contém data de atualização inválida.") from None
        return Expediente(dados["abertura"], dados["fechamento"])

    def salvar(self, config, expediente, agora):
        self.registro.salvar_documento("expediente.json", {
            "versao": 1, "loja_id": config.loja_id, "equipamento_id": config.equipamento_id,
            "abertura": expediente.abertura, "fechamento": expediente.fechamento,
            "atualizado_em": agora.isoformat(), "fuso": "-03:00"})
