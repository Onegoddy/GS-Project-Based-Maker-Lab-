"""
Módulo de CONFIGURAÇÃO da assistente M.A.D.A.R.A.

Função utilitária para ler segredos/configurações (chaves de API, ids de voz)
de duas fontes, nesta ordem:
    1) Variáveis de ambiente do sistema.
    2) Um arquivo '.env' na pasta do projeto (formato CHAVE=valor).

O '.env' é prático porque variáveis criadas por 'setx' só passam a valer em
terminais abertos DEPOIS, o que costuma confundir na hora de rodar.
O arquivo '.env' está no .gitignore e NÃO deve ser enviado ao GitHub.
"""

import os
from pathlib import Path
from dataclasses import dataclass, fields
from datetime import datetime, time as hora, timezone, timedelta
import math

RAIZ = Path(__file__).resolve().parents[1]
BRASILIA = timezone(timedelta(hours=-3), name="Brasilia")


def obter_variavel(nome: str, padrao: str | None = None) -> str | None:
    """Retorna o valor de 'nome' vindo do ambiente ou do arquivo .env."""
    # 1) Variável de ambiente.
    valor = os.getenv(nome)
    if valor:
        return valor.strip()

    # 2) Fallback: procura a chave dentro de um arquivo .env local.
    if (RAIZ / ".env").exists():
        with (RAIZ / ".env").open("r", encoding="utf-8") as arquivo:
            for linha in arquivo:
                linha = linha.strip()
                if not linha or linha.startswith("#") or "=" not in linha:
                    continue
                chave, _, bruto = linha.partition("=")
                if chave.strip() == nome:
                    # remove aspas simples/duplas ao redor do valor, se houver
                    limpo = bruto.strip().strip('"').strip("'")
                    if limpo:
                        return limpo
    return padrao


@dataclass
class ConfigEnergia:
    loja_id: str = "mercado-01"
    loja_nome: str = "Mercadinho de bairro (cenário a configurar)"
    equipamento_id: str = "iluminacao-estoque"
    ambiente: str = "estoque com pouca iluminação natural"
    abertura: str = "07:00"
    fechamento: str = "21:00"
    sensor_tipo: str = "analogico"
    referencia_escuro: int | None = None
    referencia_claro: int | None = None
    sentido: str = "auto"
    limiar: float = 0.6
    histerese: float = 0.1
    persistencia_s: float = 60
    pausa_alerta_s: float = 300
    max_sem_leitura_s: float = 3
    intervalo_s: float = 0.2
    intervalo_ia_s: float = 15
    ia_timeout_s: float = 10
    motor_aviso: bool = True
    motor_balanco_ms: int = 30
    motor_duracao_s: float = 2
    potencia_kw: float | None = None
    tarifa_rs_kwh: float | None = None

    def __post_init__(self):
        if self.sensor_tipo not in ("analogico", "digital"):
            raise ValueError("MAKER_SENSOR_TIPO deve ser analogico ou digital.")
        if self.loja_id not in ("mercado-01", "padaria-01"):
            raise ValueError("Use os IDs do guia: mercado-01 ou padaria-01.")
        if self.equipamento_id not in ("iluminacao-estoque", "iluminacao-salao"):
            raise ValueError("Equipamento de iluminação fora do contrato do guia.")
        if self.loja_id == "padaria-01" and self.equipamento_id != "iluminacao-salao":
            raise ValueError("padaria-01 usa iluminacao-salao no contrato do guia.")
        self._abre, self._fecha = hora.fromisoformat(self.abertura), hora.fromisoformat(self.fechamento)
        if self._abre == self._fecha or self._abre.tzinfo or self._fecha.tzinfo:
            raise ValueError("Informe horários locais diferentes, no formato HH:MM.")
        if (self.referencia_escuro is None) != (self.referencia_claro is None):
            raise ValueError("Configure as duas referências de calibração.")
        for valor in (self.referencia_escuro, self.referencia_claro):
            if valor is not None and (type(valor) is not int or not 0 <= valor <= self.maximo_sensor):
                faixa = "DO 0 ou 1" if self.sensor_tipo == "digital" else "ADC entre 0 e 1023"
                raise ValueError(f"Referências devem ser inteiros {faixa}. Repita --calibrar no modo selecionado.")
        if self.calibrado and self.referencia_escuro == self.referencia_claro:
            raise ValueError("As referências de escuro e claro devem ser diferentes.")
        if self.sentido not in ("auto", "crescente", "decrescente"):
            raise ValueError("Sentido: auto, crescente ou decrescente.")
        if self.calibrado:
            crescente = self.referencia_claro > self.referencia_escuro
            if self.sentido != "auto" and crescente != (self.sentido == "crescente"):
                raise ValueError("Sentido contradiz as referências de calibração.")
        if not (0 < self.histerese < self.limiar < 1 and self.limiar + self.histerese < 1):
            raise ValueError("Use 0 < histerese < limiar e limiar+histerese < 1.")
        for nome in ("persistencia_s", "pausa_alerta_s", "max_sem_leitura_s", "intervalo_s",
                     "intervalo_ia_s", "ia_timeout_s", "potencia_kw", "tarifa_rs_kwh"):
            valor = getattr(self, nome)
            if valor is not None and (not math.isfinite(valor) or valor <= 0):
                raise ValueError(f"{nome} deve ser positivo e finito.")
        if self.ia_timeout_s > 30:
            raise ValueError("Timeout da IA deve ser no máximo 30 segundos.")
        if type(self.motor_aviso) is not bool:
            raise ValueError("MAKER_MOTOR_AVISO deve ser true ou false.")
        if type(self.motor_balanco_ms) is not int or not 10 <= self.motor_balanco_ms <= 60:
            raise ValueError("MAKER_MOTOR_BALANCO_MS deve ser inteiro entre 10 e 60 ms.")
        if not math.isfinite(self.motor_duracao_s) or not 0 < self.motor_duracao_s <= 5:
            raise ValueError("MAKER_MOTOR_DURACAO_S deve ser maior que zero e no máximo 5 segundos.")

    @property
    def calibrado(self):
        return self.referencia_escuro is not None and self.referencia_claro is not None

    @property
    def maximo_sensor(self):
        return 1 if self.sensor_tipo == "digital" else 1023

    def normalizar(self, bruto):
        if not self.calibrado:
            raise ValueError("LDR ainda não calibrado.")
        return max(0., min(1., (bruto - self.referencia_escuro) /
                            (self.referencia_claro - self.referencia_escuro)))

    def loja_aberta(self, agora: datetime):
        if agora.tzinfo is None:
            raise ValueError("Use datetime com fuso horário.")
        atual = agora.astimezone(BRASILIA).time().replace(tzinfo=None)
        if self._abre < self._fecha:
            return self._abre <= atual < self._fecha
        return atual >= self._abre or atual < self._fecha

    @classmethod
    def do_ambiente(cls):
        valores = {}
        inteiros = {"referencia_escuro", "referencia_claro", "motor_balanco_ms"}
        textos = {"loja_id", "loja_nome", "equipamento_id", "ambiente", "abertura", "fechamento", "sentido", "sensor_tipo"}
        for campo in fields(cls):
            bruto = obter_variavel("MAKER_" + campo.name.upper())
            if bruto is not None:
                if campo.name == "motor_aviso":
                    if bruto.lower() not in ("true", "false", "1", "0", "sim", "nao", "não"):
                        raise ValueError("MAKER_MOTOR_AVISO deve ser true ou false.")
                    valores[campo.name] = bruto.lower() in ("true", "1", "sim")
                    continue
                valores[campo.name] = bruto if campo.name in textos else (
                    int(bruto) if campo.name in inteiros else float(bruto))
        if valores.get("loja_id") == "padaria-01":
            valores.setdefault("abertura", "06:00")
            valores.setdefault("fechamento", "20:00")
            valores.setdefault("equipamento_id", "iluminacao-salao")
        return cls(**valores)
