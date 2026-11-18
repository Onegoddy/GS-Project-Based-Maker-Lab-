"""
Pacote 'modulos' do Projeto M.A.D.A.R.A.

Reúne as classes responsáveis por cada funcionalidade da assistente:
    - Voz                  -> reconhecimento e síntese de fala
    - Agenda               -> cadastro, leitura e limpeza de eventos
    - ReconhecimentoFacial -> abre a webcam e reconhece o usuário
    - IAGenerativa         -> integração com LLMs (Groq na nuvem / Ollama local)
    - Utilidades           -> hora, data, cálculos, cotações, clima, etc.
    - JogoDaVelha          -> mini-game jogado por voz
    - Arduino              -> olhos, cabeça (motor DC) e sensor de luz

As classes são importadas sob demanda. Assim, scripts de Arduino não
precisam das bibliotecas de voz, câmera ou IA para iniciar.
"""

from importlib import import_module

_MODULOS = {
    "Voz": "voz",
    "Agenda": "agenda",
    "ReconhecimentoFacial": "reconhecimento_facial",
    "IAGenerativa": "ia_generativa",
    "Utilidades": "utilidades",
    "JogoDaVelha": "jogo_da_velha",
    "Arduino": "arduino",
    "IAIluminacao": "ia_iluminacao",
    "MonitorEnergia": "energia",
    "ServicoMonitoramento": "monitoramento_energia",
    "RegistroEnergia": "eventos",
    "ControladorVozEnergia": "dialogo_energia",
}

__all__ = list(_MODULOS)


def __getattr__(nome: str):
    """Preserva 'from modulos import Arduino' sem carregar as outras classes."""
    if nome not in _MODULOS:
        raise AttributeError(f"O módulo {__name__!r} não possui o atributo {nome!r}")
    modulo = import_module(f".{_MODULOS[nome]}", __name__)
    classe = getattr(modulo, nome)
    globals()[nome] = classe
    return classe
