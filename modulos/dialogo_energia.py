"""Interface de energia compartilhada por madara.py e pelo modo Maker com microfone."""
from contextlib import nullcontext
import math
import re
import time
from .config import obter_variavel
from .consultor_energia import (ConsultorEnergia, dentro_escopo, intervencao_eletrica,
                               RECUSA_ESCOPO, ORIENTACAO_PROFISSIONAL)
from .expediente import Expediente, interpretar_horario, extrair_horarios_frase, HorarioInvalido
from .ia_iluminacao import diagnosticar_erro_ia
from .intencoes import intencao_energia, encerrar_assistente, normalizar


class DialogoCancelado(Exception):
    pass


class ControladorVozEnergia:
    def __init__(self, monitor, voz, ia=None, *, nome="madara", relogio=time.monotonic,
                 espera_s=None, total_s=None):
        self.monitor, self.voz, self.ia = monitor, voz, ia or monitor.ia
        self.nome, self.relogio, self.ativa = nome, relogio, True
        self.espera_s = float(espera_s if espera_s is not None else obter_variavel("MAKER_DIALOGO_ESPERA_S", "8"))
        self.total_s = float(total_s if total_s is not None else obter_variavel("MAKER_DIALOGO_TOTAL_S", "120"))
        if (not math.isfinite(self.espera_s) or not 1 <= self.espera_s <= 30
                or not math.isfinite(self.total_s) or not 15 <= self.total_s <= 300):
            raise ValueError("Janela de resposta deve ser 1..30 s e diálogo total 15..300 s.")

    def _sem_nome(self, texto):
        return re.sub(rf"\b{re.escape(self.nome)}\b", "", texto, flags=re.I).strip(" ,.!?")

    def processar_fala(self, texto):
        if not re.search(rf"\b{re.escape(self.nome)}\b", texto, re.I):
            return False
        comando = self._sem_nome(texto)
        if encerrar_assistente(comando):
            self.voz.falar("Encerrando a interface de energia e o monitoramento.")
            self.ativa = False
            return True
        self.processar_comando(comando)
        return True

    def executar(self, parar=None, fim=None):
        self.voz.falar("Comandos de energia por microfone ativos. Comece cada comando com Madara.")
        while self.ativa and (parar is None or not parar.is_set()) and (fim is None or self.relogio() < fim):
            try:
                fala = self.voz.ouvir(timeout=1, limite_frase=15)
            except (OSError, RuntimeError) as erro:
                raise RuntimeError("Microfone indisponível. Confira o dispositivo e requirements-microfone.txt.") from erro
            if fala:
                self.processar_fala(fala)

    def _capturar(self, fim):
        restante = fim - self.relogio()
        if restante <= 0:
            raise DialogoCancelado("O tempo do diálogo terminou. O expediente não foi alterado.")
        try:
            texto = self.voz.ouvir(timeout=min(self.espera_s, restante), limite_frase=min(15, restante))
        except (OSError, RuntimeError):
            raise DialogoCancelado("A captura falhou. O expediente não foi alterado.") from None
        if self.relogio() > fim or not texto:
            raise DialogoCancelado("Não recebi uma resposta dentro da janela. O expediente não foi alterado.")
        texto = self._sem_nome(texto)
        t = normalizar(texto)
        if t in {"cancelar", "cancela", "cancele", "cancelar configuracao", "nao", "nao confirmo", "nao confirmar", "nao salve", "deixe como estava", "deixa como estava"}:
            raise DialogoCancelado("Configuração cancelada. O expediente não foi alterado.")
        return texto

    def _pedir_horario(self, campo, fim, inicial=None):
        valor = inicial
        for tentativa in range(2):
            if valor is None:
                pergunta = "Qual é o horário de abertura da loja?" if campo == "abertura" else "Qual é o horário de fechamento da loja?"
                self.voz.falar(pergunta + " Informe o período ou use o formato de vinte e quatro horas.")
                valor = self._capturar(fim)
            try:
                return interpretar_horario(valor)
            except HorarioInvalido as erro:
                self.voz.falar(str(erro))
                valor = None
        raise DialogoCancelado("Não consegui determinar o horário sem ambiguidade. O expediente não foi alterado.")

    def configurar_expediente(self, comando):
        anterior = self.monitor.consultar_expediente()
        partes = extrair_horarios_frase(comando)
        sessao = self.voz.dialogo() if hasattr(self.voz, "dialogo") else nullcontext()
        with sessao:
            fim = self.relogio() + self.total_s
            try:
                self.voz.falar(f"Vamos configurar o expediente da loja. Durante este ajuste, pode responder sem Madara. "
                              f"Comece cada resposta em até {self.espera_s:g} segundos. O diálogo dura no máximo {self.total_s:g} segundos. Diga cancelar para sair.")
                abertura = self._pedir_horario("abertura", fim, partes[0] if partes else None)
                fechamento = self._pedir_horario("fechamento", fim, partes[1] if partes else None)
                expediente = Expediente(abertura, fechamento)
                noite = " O fechamento é no dia seguinte." if expediente.atravessa_meia_noite else ""
                self.voz.falar(f"Entendi: abertura às {abertura} e fechamento às {fechamento}, no horário de Brasília.{noite} "
                              "Diga confirmo para salvar ou cancelar para manter o expediente atual.")
                confirmado = False
                for tentativa in range(2):
                    resposta = normalizar(self._capturar(fim))
                    if resposta in {"sim", "confirmo", "confirmar", "sim confirmo", "pode salvar", "sim pode salvar",
                                    "pode aplicar", "pode confirmar", "esta correto", "correto", "confirmo os horarios"}:
                        confirmado = True
                        break
                    if tentativa == 0:
                        self.voz.falar("A confirmação não ficou clara. Diga confirmo ou cancelar.")
                if not confirmado:
                    raise DialogoCancelado("Não houve confirmação explícita. O expediente não foi alterado.")
                resultado = self.monitor.atualizar_expediente(abertura, fechamento, confirmado=True,
                                                            revisao_esperada=anterior["revisao"])
                mensagem = f"Expediente salvo: das {abertura} às {fechamento}.{noite} O monitoramento mantém seu estado de pausa ou atividade."
                if resultado["ocorrencia_encerrada"]:
                    mensagem += " A observação anterior foi encerrada por mudança de expediente."
                if resultado["auditoria_pendente"]:
                    mensagem += " O horário foi aplicado, mas o registro da auditoria está pendente. Confira o terminal."
                self.voz.falar(mensagem)
            except (DialogoCancelado, HorarioInvalido) as erro:
                self.voz.falar(str(erro))
            except (OSError, ValueError):
                self.voz.falar("Não consegui salvar ou confirmar a atualização. Consulte o expediente atual e confira o arquivo de configuração no terminal.")

    def processar_comando(self, comando, intencao=None):
        intencao = intencao or intencao_energia(comando)
        if intencao == "configurar_expediente":
            self.configurar_expediente(comando)
            return
        if intencao == "expediente":
            e = self.monitor.consultar_expediente()
            mensagem = f"O expediente configurado é das {e['abertura']} às {e['fechamento']}, horário de Brasília."
            if e["atravessa_meia_noite"]:
                mensagem += " O fechamento é no dia seguinte."
        elif intencao == "pausar":
            self.monitor.pausar()
            mensagem = self.monitor.descrever()
        elif intencao == "retomar":
            self.monitor.retomar()
            mensagem = "Monitoramento retomado. O expediente da loja permanece configurado como antes."
        elif intencao == "resumo":
            mensagem = self.monitor.resumo()
            print(self.monitor.diagnostico())
        elif intencao == "ultimo_alerta":
            alerta = self.monitor.consultar_ultimo_alerta()
            if alerta is None:
                mensagem = "Não há alerta disponível no histórico local deste ambiente."
            else:
                analise = alerta.get("analise") or {}
                fonte = alerta.get("fonte")
                prefixo = "Aviso de contingência determinística, sem análise válida da IA. " if fonte == "contingencia_deterministica" else "Último alerta local. "
                mensagem = prefixo + alerta["mensagem"]
                if analise.get("justificativa"):
                    mensagem += " Justificativa registrada: " + analise["justificativa"]
                if not alerta.get("ocorrencia_ativa"):
                    mensagem += " A observação dessa ocorrência já foi encerrada; este é um alerta histórico."
        elif intencao in ("consulta", "custo", "parametros", "consumo"):
            contexto = self.monitor.contexto_energia()
            if intencao == "custo":
                mensagem = self._custo(contexto, ultimo_alerta="ultimo alerta" in normalizar(comando))
            elif intencao == "parametros":
                p = contexto["parametros"]
                mensagem = (f"Potência cadastrada: {p['potencia_kw']} quilowatts. " if p["potencia_kw"] is not None else "Potência não cadastrada. ")
                mensagem += (f"Tarifa cadastrada: {p['tarifa_rs_kwh']} reais por quilowatt-hora." if p["tarifa_rs_kwh"] is not None else "Tarifa não cadastrada. Não consulto a tarifa atual da distribuidora automaticamente.")
            elif intencao == "consumo":
                mensagem = "Este robô não mede consumo elétrico ou quilowatt-hora. O LDR observa apenas a condição de iluminação; custos usam parâmetros cadastrados e duração observada."
            else:
                m = contexto["monitor"]
                if m["pausado"]:
                    mensagem = "Monitoramento pausado; a coleta continua, sem novas ocorrências."
                elif not m["disponivel"]:
                    mensagem = "Iluminação indisponível: não há leitura válida recente."
                elif not m["calibrado"]:
                    mensagem = "O sensor ainda não foi calibrado. Não posso interpretar claro ou escuro."
                elif m["ambiente_claro"]:
                    mensagem = ("O sensor indica condição iluminada durante o expediente; isso não caracteriza desperdício automaticamente." if contexto["loja_aberta"] else "O sensor indica claridade fora do expediente. Persistência e análise contextual orientam o aviso.")
                else:
                    mensagem = "O sensor indica condição escura conforme a calibração."
                if m["persistencia_s"] is not None:
                    mensagem += f" Claridade observada nesta condição por {m['persistencia_s']} segundos."
        elif intencao == "conversar":
            self._conversar(comando)
            return
        else:
            mensagem = RECUSA_ESCOPO
        self.voz.falar(mensagem)

    @staticmethod
    def _custo(contexto, ultimo_alerta=False):
        o = (contexto["ocorrencia_ultimo_alerta"] if ultimo_alerta
             else contexto["ocorrencia_atual"] or contexto["ultima_ocorrencia"])
        if o is None:
            return "Não há ocorrência disponível para estimar o custo. O programa usa potência cadastrada, duração observada e tarifa, sem medir consumo elétrico."
        if o["custo_estimado_rs"] is None:
            return "O custo desta ocorrência está indisponível: faltam potência e/ou tarifa daquele registro. Desconhecido não é custo zero."
        valor = f"{o['custo_estimado_rs']:.8f}".rstrip("0").rstrip(".").replace(".", ",")
        mensagem = f"Estimativa calculada pelo programa: R$ {valor}, para {o['duracao_s']} segundos observados. Não é consumo medido nem economia comprovada."
        if o["ativo"]:
            mensagem += " A ocorrência está ativa; este custo é parcial."
        if o["origem"] == "simulador":
            mensagem += " O registro é simulado."
        return mensagem

    def _conversar(self, pergunta):
        if not dentro_escopo(pergunta):
            self.voz.falar(RECUSA_ESCOPO)
            return
        if intervencao_eletrica(pergunta):
            self.voz.falar(ORIENTACAO_PROFISSIONAL)
            return
        real = self.monitor.contexto_energia()
        # Perguntas gerais não recebem o histórico completo de ocorrências.
        contexto = {"tipo": "informacao_geral", "limites": real["limites"]}
        if re.search(r"estoque|ambiente|minha loja|nosso|aqui", normalizar(pergunta)):
            contexto.update(loja_id=real["loja_id"], loja_nome=real["loja_nome"], ambiente=real["ambiente"],
                            monitor=real["monitor"], expediente=real["expediente"], loja_aberta=real["loja_aberta"])
        try:
            resposta = self.ia.responder_energia(pergunta, contexto, timeout_s=self.monitor.config.ia_timeout_s)
            resposta = ConsultorEnergia.validar(resposta, contexto)
        except Exception as erro:
            falha = diagnosticar_erro_ia(erro)
            print(f"[Consultor de energia] {falha['codigo']}: {falha['orientacao']}")
            self.voz.falar("Não consegui uma resposta validada do consultor de energia. As consultas locais de iluminação, expediente, alertas e custos continuam disponíveis.")
            return
        prefixo = "Informação geral: " if resposta["tipo"] == "informacao_geral" else ""
        self.voz.falar(prefixo + resposta["resposta"])
