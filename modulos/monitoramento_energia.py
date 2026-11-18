"""LDR + horário + persistência. Avaliação temporal nunca espera IA ou áudio."""

from dataclasses import dataclass, replace
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timedelta
import math
import queue
import threading
import time
from uuid import uuid4
from .config import BRASILIA, ConfigEnergia, obter_variavel
from .eventos import RegistroEnergia, estimar_custo
from .expediente import ArquivoExpediente, Expediente
from .ia_generativa import IAGenerativa
from .ia_iluminacao import IAIluminacao, diagnosticar_erro_ia


@dataclass
class ResultadoEnergia:
    estado: str
    mensagem: str


class MonitorEnergia:
    """Máquina temporal testável; filas separam análise e avisos da avaliação."""

    def __init__(self, ia=None, config=None, registro=None, simulado=False,
                 relogio=time.monotonic, agora=None):
        self.config = config or ConfigEnergia.do_ambiente()
        self.registro = registro or RegistroEnergia()
        self._arquivo_expediente = ArquivoExpediente(self.registro)
        salvo = self._arquivo_expediente.carregar(self.config)
        if salvo:
            self.config = replace(self.config, abertura=salvo.abertura, fechamento=salvo.fechamento)
        self._fonte_expediente = "expediente.json" if salvo else "configuracao_inicial"
        self._revisao_expediente = 0
        self.ia = ia or IAGenerativa(provedor=obter_variavel("MAKER_IA_PROVEDOR", "groq"))
        self.simulado, self.relogio = simulado, relogio
        self.agora = agora or (lambda: datetime.now(BRASILIA))
        self.pedidos = queue.Queue(maxsize=8)
        self.avisos = queue.Queue(maxsize=8)
        self._trava = threading.RLock()
        self._ocorrencias = {}
        self._resultados_pendentes = {}
        self._auditorias_pendentes = []
        self._ultima_ocorrencia = None
        self._dados_disponiveis = False
        self._ativa = None
        self._inicio = self._inicio_em = None
        self._claro = False
        self._ultimo_alerta = self._proxima_ia = float("-inf")
        self._ultima_amostra = self.ultimo_valido = None
        self.bruto = self.normalizado = None
        self.ultima_analise = self.ultimo_alerta = None
        self.estado = "indisponivel"
        self.pausado = False
        self.total_ocorrencias = 0
        self._recuperar()
        self._eventos_historicos, self.ultimo_alerta, self._analises_historicas = self.registro.historico_energia(
            self.config.loja_id, self.config.equipamento_id)
        self._ultima_ocorrencia = max(self._eventos_historicos.values(), key=lambda o: o["inicio"], default=None)
        if self._analises_historicas:
            self.ultima_analise = list(self._analises_historicas.values())[-1]
        if salvo:
            self._auditar("expediente_carregado", abertura=salvo.abertura, fechamento=salvo.fechamento)

    def _recuperar(self):
        # Duração do checkpoint; monotônicos nunca são subtraídos entre processos.
        for o in self.registro.carregar_ativos():
            self.registro.finalizar(o)
            self._auditar("interrompida_reinicio", evento_id=o["id"], duracao_s=o["duracao_s"],
                          analise_concluida=o.get("analise") is not None)
        self.registro.salvar_ativos([])

    def _auditar(self, estado, **dados):
        self.registro.gravar("auditoria.jsonl", {
            "em": self.agora().isoformat(), "estado": estado,
            "origem": "simulador" if self.simulado else "maker", **dados})

    def _estado(self, estado):
        if estado != self.estado:
            self._auditar(estado)
            self.estado = estado
        return ResultadoEnergia(self.estado, self.descrever())

    def _checkpoint(self):
        self.registro.salvar_ativos([{k: v for k, v in o.items() if not k.endswith("_monotonico")}
                                     for o in self._ocorrencias.values()])

    def _enfileirar_pendentes(self, instante):
        if instante < self._proxima_ia:
            return
        for o in self._ocorrencias.values():
            if o["enfileirada"] or "contexto" not in o:
                continue
            try:
                self.pedidos.put_nowait((o["id"], o["contexto"], o["amostra_monotonico"]))
            except queue.Full:
                return  # preserva pendência e checkpoint, tenta na próxima avaliação
            o["enfileirada"] = True
            self._proxima_ia = instante + self.config.intervalo_ia_s
            return

    def _concluir(self, instante, motivo):
        if self._ativa:
            o = self._ocorrencias[self._ativa]
            o.update(duracao_s=max(1, int(instante - o["inicio_monotonico"])),
                     encerrada=True, fim_motivo=motivo)
            self._lembrar_ocorrencia(o)
            self._auditar("ocorrencia_encerrada", evento_id=o["id"], motivo=motivo,
                          duracao_s=o["duracao_s"])
            # IA pode terminar após a resolução; nesse caso registra, sem fala tardia.
            if o["analise"] is not None:
                self.registro.finalizar(o)
                del self._ocorrencias[o["id"]]
            self._ativa = None
            self._checkpoint()
        self._inicio = self._inicio_em = None

    def atualizar(self, bruto, agora=None, instante=None, *, leitura_instante=None, disponivel=True):
        """Não chama IA. Cache conserva o instante original de recepção da amostra."""
        with self._trava:
            agora = agora or self.agora()
            if agora.tzinfo is None:
                raise ValueError("Registros precisam de data/hora com fuso.")
            instante = self.relogio() if instante is None else instante
            self._enfileirar_pendentes(instante)
            amostra = instante if leitura_instante is None else leitura_instante
            cfg = self.config
            valido = (disponivel and type(bruto) is int and 0 <= bruto <= cfg.maximo_sensor and
                      0 <= instante - amostra <= cfg.max_sem_leitura_s)
            self._dados_disponiveis = valido
            if not valido:
                self._concluir(self.ultimo_valido if self.ultimo_valido is not None else instante,
                               "dados_indisponiveis")
                self._claro = False
                return self._estado("pausado" if self.pausado else "indisponivel")
            if self.ultimo_valido is not None and amostra - self.ultimo_valido > cfg.max_sem_leitura_s:
                self._concluir(self.ultimo_valido, "lacuna_de_dados")
                self._claro = False
            self.bruto, self.ultimo_valido = bruto, amostra
            if amostra != self._ultima_amostra:
                self.registro.gravar("leituras.jsonl", {
                    "em": agora.isoformat(), "bruto": bruto, "idade_s": instante - amostra,
                    "sensor_tipo": cfg.sensor_tipo, "pino": "8" if cfg.sensor_tipo == "digital" else "A0",
                    "origem": "simulador" if self.simulado else "maker"})
                self._ultima_amostra = amostra
            if self.pausado:
                return self._estado("pausado")
            if not cfg.calibrado:
                return self._estado("nao_calibrado")
            self.normalizado = cfg.normalizar(bruto)
            if self.normalizado >= cfg.limiar + cfg.histerese:
                self._claro = True
            elif self.normalizado <= cfg.limiar - cfg.histerese:
                self._claro = False
            aberta = cfg.loja_aberta(agora)
            if not self._claro or aberta:
                self._concluir(instante, "escureceu" if not self._claro else "loja_abriu")
                return self._estado("ambiente_escuro" if not self._claro else "dentro_do_horario")
            if self._inicio is None:
                self._inicio, self._inicio_em = instante, agora.isoformat()
            duracao = max(0, instante - self._inicio)
            if duracao < cfg.persistencia_s:
                return self._estado("observando")
            if self._ativa is None:
                o = {"id": str(uuid4()), "loja_id": cfg.loja_id, "equipamento_id": cfg.equipamento_id,
                     "origem": "simulador" if self.simulado else "maker", "inicio": self._inicio_em,
                     "inicio_monotonico": self._inicio, "duracao_s": max(1, int(duracao)),
                     "potencia_kw": cfg.potencia_kw, "tarifa_rs_kwh": cfg.tarifa_rs_kwh,
                     "registrar": False, "encerrada": False, "analise": None,
                     "enfileirada": False, "avisada": False}
                self._ocorrencias[o["id"]] = o
                self._ativa = o["id"]
                self.total_ocorrencias += 1
                self._auditar("ocorrencia_iniciada", evento_id=o["id"])
            o = self._ocorrencias[self._ativa]
            o["duracao_s"] = max(1, int(duracao))
            contexto = {
                "loja_id": cfg.loja_id, "loja": cfg.loja_nome, "ambiente": cfg.ambiente,
                "equipamento_id": cfg.equipamento_id, "agora": agora.isoformat(),
                "sensor_tipo": cfg.sensor_tipo, "pino": "8" if cfg.sensor_tipo == "digital" else "A0",
                "leitura_adc": bruto if cfg.sensor_tipo == "analogico" else None,
                "leitura_digital": bruto if cfg.sensor_tipo == "digital" else None,
                "luminosidade_relativa": self.normalizado if cfg.sensor_tipo == "analogico" else None,
                "ambiente_claro": self._claro,
                "condicao_calibrada": "iluminado" if self._claro else "escuro",
                "interpretacao_sensor": (
                    f"DO={bruto} corresponde a ambiente {'iluminado' if self._claro else 'escuro'} nesta calibração; "
                    f"escuro={cfg.referencia_escuro}, iluminado={cfg.referencia_claro}. DO não mede intensidade."
                    if cfg.sensor_tipo == "digital" else "ADC interpretado pela escala relativa calibrada; não é lux."),
                "referencia_escuro": cfg.referencia_escuro, "referencia_claro": cfg.referencia_claro,
                "sentido": "crescente" if cfg.referencia_claro > cfg.referencia_escuro else "decrescente",
                "limiar": cfg.limiar, "histerese": cfg.histerese,
                "revisao_expediente": self._revisao_expediente,
                "loja_aberta": aberta, "abertura": cfg.abertura, "fechamento": cfg.fechamento,
                "persistencia_s": round(duracao, 3), "persistencia_minima_s": cfg.persistencia_s,
                "persistencia_confirmada": duracao >= cfg.persistencia_s,
                "criterio_atencao": "claridade_persistente_fora_expediente",
                "qualidade": "valida_e_calibrada",
                "idade_leitura_s": instante - amostra, "max_idade_s": cfg.max_sem_leitura_s,
                "presenca": "nao_medida", "origem_da_luz": "desconhecida",
                "consumo_eletrico": "nao_medido", "simulado": self.simulado}
            o["contexto"], o["amostra_monotonico"] = contexto, amostra
            self._enfileirar_pendentes(instante)
            self._emitir_aviso(o, instante)
            self._checkpoint()
            estado = o["analise"]["classificacao"] if o["analise"] else "aguardando_analise"
            return self._estado(estado)

    def analisar_pedido(self, pedido):
        """Chamado pela tarefa de IA ou pelo ensaio simulado, fora da tarefa temporal."""
        evento_id, contexto, amostra = pedido
        with self._trava:
            o = self._ocorrencias.get(evento_id)
            encerrada = o is None or o["encerrada"]
            if o is not None and "contexto" in o:
                contexto = dict(o["contexto"])
                amostra = o["amostra_monotonico"]
        idade = max(0, self.relogio() - amostra)
        contexto = {**contexto, "idade_leitura_s": idade, "ocorrencia_encerrada": encerrada,
                    "qualidade": "historica" if encerrada else (
                        "desatualizada" if idade > self.config.max_sem_leitura_s else "valida_e_calibrada")}
        erro_tipo = None
        falha = None
        decisao_recebida = rejeitada = None
        try:
            decisao_recebida = IAIluminacao.validar(self.ia.analisar_iluminacao(
                contexto, timeout_s=self.config.ia_timeout_s))
            decisao = IAIluminacao.conferir_contexto(decisao_recebida, contexto)
            fonte = "ia_simulada" if self.simulado else "ia"
        except Exception as erro:
            rejeitada = decisao_recebida
            erro_tipo = type(erro).__name__
            falha = diagnosticar_erro_ia(erro)
            http = f" HTTP {falha['http_status']}" if falha["http_status"] else ""
            print(f"[IA] {falha['codigo']}{http}: {falha['orientacao']}")
            fonte = "contingencia_deterministica"
            decisao = {
                "classificacao": "possivel_desperdicio",
                "justificativa": "Claridade persistente fora do horário. IA indisponível ou resposta não validada para este contexto.",
                "recomendacao": "Verifique a origem da claridade e se a iluminação é necessária.",
                "mensagem_falada": "Análise por IA não validada. Há claridade persistente fora do horário. Verifique a necessidade da iluminação."}
        print(f"[Energia] Análise {evento_id[:8]}: {decisao['classificacao']} | fonte={fonte}.")
        execucao = getattr(self.ia, "ultima_execucao_iluminacao", None)
        execucao = deepcopy(execucao) if type(execucao) is dict else None
        with self._trava:
            self.ultima_analise = {"evento_id": evento_id, "fonte": fonte, **decisao}
            if execucao is not None:
                self.ultima_analise["execucao_ia"] = execucao
            if falha:
                self.ultima_analise["falha"] = falha
            self._resultados_pendentes[evento_id] = {
                "analise": self.ultima_analise, "contexto": contexto,
                "erro_tipo": erro_tipo, "falha": falha, "execucao_ia": execucao,
                "decisao_ia_rejeitada": rejeitada, "auditado": False}
            self._analises_historicas[evento_id] = deepcopy(self.ultima_analise)
            self.regravar_resultados()

    def regravar_resultados(self):
        """Retenta armazenamento de resultados em memória, sem repetir a chamada de IA."""
        with self._trava:
            alterou = bool(self._auditorias_pendentes)
            while self._auditorias_pendentes:
                registro = self._auditorias_pendentes[0]
                self._auditar(registro["estado"], **registro["dados"])
                self._auditorias_pendentes.pop(0)
            for evento_id, resultado in list(self._resultados_pendentes.items()):
                analise = resultado["analise"]
                if not resultado["auditado"]:
                    decisao = {k: analise[k] for k in ("classificacao", "justificativa", "recomendacao", "mensagem_falada")}
                    self._auditar("analise", evento_id=evento_id, fonte=analise["fonte"],
                        contexto=resultado["contexto"], decisao=decisao,
                        erro_tipo=resultado["erro_tipo"], falha=resultado["falha"],
                        execucao_ia=resultado.get("execucao_ia"),
                        decisao_ia_rejeitada=resultado.get("decisao_ia_rejeitada"))
                    resultado["auditado"] = True
                o = self._ocorrencias.get(evento_id)
                if o is not None:
                    o["analise"] = analise
                    o["registrar"] = analise["classificacao"] == "possivel_desperdicio"
                    if o["encerrada"]:
                        self._lembrar_ocorrencia(o)
                        self.registro.finalizar(o)
                        del self._ocorrencias[evento_id]
                self._checkpoint()
                del self._resultados_pendentes[evento_id]
            for evento_id, o in list(self._ocorrencias.items()):
                if o["encerrada"] and o["analise"] is not None:
                    self._lembrar_ocorrencia(o)
                    self.registro.finalizar(o)
                    del self._ocorrencias[evento_id]
                    alterou = True
            if alterou:
                self._checkpoint()

    def consultar_expediente(self):
        with self._trava:
            return {"abertura": self.config.abertura, "fechamento": self.config.fechamento,
                    "atravessa_meia_noite": self.config._abre > self.config._fecha,
                    "fuso": "-03:00", "fonte": self._fonte_expediente,
                    "revisao": self._revisao_expediente}

    def atualizar_expediente(self, abertura, fechamento, *, confirmado=False, revisao_esperada=None):
        """Confirma arquivo primeiro; depois troca configuração inteira sob a trava do avaliador."""
        if confirmado is not True:
            raise ValueError("A alteração do expediente exige confirmação explícita.")
        expediente = Expediente(abertura, fechamento)
        with self._trava:
            if revisao_esperada is not None and revisao_esperada != self._revisao_expediente:
                raise ValueError("O expediente mudou durante o diálogo. Consulte os horários e configure novamente.")
            anterior = self.consultar_expediente()
            nova = replace(self.config, abertura=expediente.abertura, fechamento=expediente.fechamento)
            # Falha aqui deixa o arquivo anterior e a configuração em execução intactos.
            self._arquivo_expediente.salvar(nova, expediente, self.agora())
            self.config, self._fonte_expediente = nova, "expediente.json"
            self._revisao_expediente += 1
            mudou = (anterior["abertura"], anterior["fechamento"]) != (abertura, fechamento)
            encerrada = None
            if mudou:
                if self._ativa:
                    o = self._ocorrencias[self._ativa]
                    fim = self.ultimo_valido if self.ultimo_valido is not None else self.relogio()
                    o.update(encerrada=True, fim_motivo="expediente_alterado",
                             duracao_s=max(1, int(fim - o["inicio_monotonico"])))
                    encerrada = o["id"]
                    self._lembrar_ocorrencia(o)
                    self._ativa = None
                self._inicio = self._inicio_em = None
                self._claro = False
                self._dados_disponiveis = False
                self.estado = "pausado" if self.pausado else "indisponivel"
            self._auditorias_pendentes.append({"estado": "expediente_alterado" if mudou else "expediente_confirmado",
                "dados": {"anterior": anterior, "novo": self.consultar_expediente(),
                          "ocorrencia_encerrada": encerrada, "motivo": "confirmacao_por_voz",
                          "loja_id": nova.loja_id, "equipamento_id": nova.equipamento_id}})
            pendente = False
            try:
                self.regravar_resultados()
                self._checkpoint()
            except OSError:
                # O arquivo foi confirmado: nunca informa ao usuário que o horário antigo segue ativo.
                pendente = True
                self.estado = "falha_registro"
            return {**self.consultar_expediente(), "auditoria_pendente": pendente,
                    "ocorrencia_encerrada": encerrada}

    def _visao_ocorrencia(self, o, ativo=False):
        duracao = o["duracao_s"]
        if ativo:
            fim = min(self.relogio(), self.ultimo_valido) if self.ultimo_valido is not None else o["inicio_monotonico"]
            duracao = max(1, int(fim - o["inicio_monotonico"]))
        return {"id": o["id"], "inicio": o["inicio"], "origem": o["origem"],
                "duracao_s": duracao, "ativo": ativo, "analise": deepcopy(o.get("analise")),
                "custo_estimado_rs": estimar_custo(o.get("potencia_kw"), duracao, o.get("tarifa_rs_kwh")),
                "fonte_dados": "monitor_memoria", "motivo_encerramento": o.get("fim_motivo"),
                "potencia_kw": o.get("potencia_kw"), "tarifa_rs_kwh": o.get("tarifa_rs_kwh")}

    def _lembrar_ocorrencia(self, o):
        visao = self._visao_ocorrencia(o)
        self._eventos_historicos[o["id"]] = visao
        if self._ultima_ocorrencia is None or visao["inicio"] >= self._ultima_ocorrencia["inicio"]:
            self._ultima_ocorrencia = visao

    def consultar_ultimo_alerta(self):
        with self._trava:
            if self.ultimo_alerta is None:
                return None
            alerta = deepcopy(self.ultimo_alerta)
            eid = alerta["evento_id"]
            alerta["analise"] = deepcopy(self._analises_historicas.get(eid))
            alerta["ocorrencia_ativa"] = self._ativa == eid
            return alerta

    def contexto_energia(self):
        """Snapshot local, sem I/O, microfone, LLM ou credenciais. Leitura antiga nunca vira normal."""
        with self._trava:
            idade = None if self.ultimo_valido is None else max(0, self.relogio() - self.ultimo_valido)
            disponivel = self._dados_disponiveis and idade is not None and idade <= self.config.max_sem_leitura_s
            estado = self.estado if disponivel or self.pausado else "indisponivel"
            atual = self._visao_ocorrencia(self._ocorrencias[self._ativa], True) if self._ativa else None
            alerta_id = self.ultimo_alerta["evento_id"] if self.ultimo_alerta else None
            o_alerta = self._ocorrencias.get(alerta_id)
            custo_alerta = (self._visao_ocorrencia(o_alerta, alerta_id == self._ativa) if o_alerta
                           else self._eventos_historicos.get(alerta_id))
            return deepcopy({"agora": self.agora().isoformat(), "loja_id": self.config.loja_id,
                "loja_nome": self.config.loja_nome,
                "ambiente": self.config.ambiente, "equipamento_id": self.config.equipamento_id,
                "expediente": self.consultar_expediente(), "loja_aberta": self.config.loja_aberta(self.agora()),
                "monitor": {"estado": estado, "pausado": self.pausado, "disponivel": disponivel,
                    "calibrado": self.config.calibrado, "sensor_tipo": self.config.sensor_tipo,
                    "bruto": self.bruto if disponivel else None, "idade_s": idade,
                    "ambiente_claro": self._claro if disponivel and self.config.calibrado and not self.pausado else None,
                    "persistencia_s": max(0, int(self.relogio() - self._inicio)) if disponivel and self._inicio is not None else None},
                "ocorrencia_atual": atual, "ultima_ocorrencia": self._ultima_ocorrencia,
                "ocorrencia_ultimo_alerta": custo_alerta,
                "ultimo_alerta": self.consultar_ultimo_alerta(),
                "parametros": {"potencia_kw": self.config.potencia_kw, "tarifa_rs_kwh": self.config.tarifa_rs_kwh},
                "limites": {"consumo_medido": False, "presenca_medida": False, "lampada_ligada_comprovada": False}})

    def _emitir_aviso(self, o, instante):
        if not o["registrar"] or o["avisada"]:
            return
        restante = self.config.pausa_alerta_s - (instante - self._ultimo_alerta)
        if restante > 0:
            if not o.get("adiamento_aviso_registrado"):
                segundos = math.ceil(restante)
                self._auditar("aviso_adiado_intervalo", evento_id=o["id"], restante_s=segundos,
                    intervalo_minimo_s=self.config.pausa_alerta_s,
                    previsao_em=(self.agora() + timedelta(seconds=restante)).isoformat())
                o["adiamento_aviso_registrado"] = True
                print(f"[Energia] Aviso {o['id'][:8]} aguardando intervalo mínimo: faltam {segundos} s. "
                      "Será solicitado se a condição continuar ativa.")
            return
        analise = o["analise"]
        aviso = {"evento_id": o["id"], "mensagem": "Possível desperdício de iluminação. " + analise["mensagem_falada"],
                 "fonte": analise["fonte"], "em": self.agora().isoformat()}
        self._auditar("aviso_solicitado", **aviso)
        try:
            self.avisos.put_nowait(aviso)
        except queue.Full:
            return
        o["avisada"] = True
        self._ultimo_alerta = instante
        self.ultimo_alerta = aviso

    def aviso_atual(self, evento_id):
        with self._trava:
            return not self.pausado and self._ativa == evento_id and self.estado == "possivel_desperdicio"

    def pausar(self):
        with self._trava:
            self._concluir(self.ultimo_valido if self.ultimo_valido is not None else self.relogio(), "pausa_solicitada")
            self.pausado = True
            self._estado("pausado")

    def retomar(self):
        with self._trava:
            self.pausado = False
            self._inicio = self._inicio_em = None
            self._claro = False
            self._dados_disponiveis = False
            self._estado("indisponivel")

    def encerrar(self):
        with self._trava:
            self.regravar_resultados()
            self._concluir(self.ultimo_valido if self.ultimo_valido is not None else self.relogio(), "monitor_encerrado")
            for o in list(self._ocorrencias.values()):
                self.registro.finalizar(o)
                if o["analise"] is None:
                    self._auditar("analise_nao_concluida", evento_id=o["id"])
            self._ocorrencias.clear()
            self._checkpoint()

    def descrever(self):
        return {
            "indisponivel": "Iluminação indisponível: sem leitura válida recente ou sem suporte ao sensor configurado.",
            "nao_calibrado": "LDR sem calibração. Configure as referências de escuro e iluminado.",
            "pausado": "Monitoramento pausado; a coleta continua, sem novas ocorrências.",
            "ambiente_escuro": "O LDR indica condição escura conforme a calibração.",
            "dentro_do_horario": "Ambiente iluminado durante o funcionamento; isso não indica desperdício por si só.",
            "observando": "Claridade fora do horário. Aguardando persistência mínima.",
            "aguardando_analise": "Claridade persistente fora do horário; análise solicitada ou em espera.",
            "possivel_desperdicio": "Há indício de possível desperdício; verifique a origem da claridade.",
            "normal": "A IA não identificou possível desperdício neste contexto; confira a justificativa no diagnóstico.",
            "dados_insuficientes": "A IA indicou dados insuficientes para avaliar esta ocorrência.",
            "falha_registro": "Falha ao registrar dados. Confira pasta e espaço em disco.",
        }.get(self.estado, self.estado)

    def resumo(self):
        with self._trava:
            custo = ("Custo estimado configurado." if self.config.potencia_kw is not None and
                     self.config.tarifa_rs_kwh is not None else "Custo indisponível: configure potência e tarifa.")
            aviso = self._situacao_aviso()
            espera = (f" O aviso aguarda o intervalo mínimo; faltam {aviso['restante_s']} segundos."
                      if aviso["estado"] == "aguardando_intervalo" else "")
            return (f"{self.descrever()} Ocorrências nesta execução: {self.total_ocorrencias}. "
                    f"{custo}{espera} O LDR não mede consumo nem comprova lâmpada ligada.")

    def _situacao_aviso(self):
        """Chamado sob a trava. Solicitado significa enfileirado, não reprodução comprovada."""
        o = self._ocorrencias.get(self._ativa)
        if o is None:
            return {"estado": "sem_ocorrencia", "restante_s": None}
        if o["analise"] is None:
            return {"estado": "aguardando_analise", "restante_s": None}
        if not o["registrar"]:
            return {"estado": "sem_aviso_para_classificacao", "restante_s": None}
        if o["avisada"]:
            return {"estado": "solicitado", "restante_s": 0}
        restante = math.ceil(max(0, self.config.pausa_alerta_s - (self.relogio() - self._ultimo_alerta)))
        return {"estado": "aguardando_intervalo" if restante else "pronto_para_aviso", "restante_s": restante}

    def diagnostico(self):
        with self._trava:
            return {"bruto": self.bruto, "estado": self.estado,
                    "sensor_tipo": self.config.sensor_tipo,
                    "idade_s": None if self.ultimo_valido is None else round(self.relogio() - self.ultimo_valido, 2),
                    "analise": self.ultima_analise, "ocorrencia": self._ativa,
                    "aviso": self._situacao_aviso(),
                    "persistencia_s": None if self._inicio is None else max(0, int(self.relogio() - self._inicio))}


class ServicoMonitoramento:
    """Avaliação, IA e avisos independentes; nenhuma consulta na tarefa serial."""

    def __init__(self, arduino, voz=None, monitor=None):
        self.arduino, self.voz = arduino, voz
        self.monitor = monitor or MonitorEnergia()
        self._parar = threading.Event()
        self._threads = []
        self._ultima_falha = None
        self._ultima_falha_em = float("-inf")
        self._proxima_regravacao = 0

    def iniciar(self):
        if self._threads:
            return self
        for nome, alvo in (("energia-avaliacao", self._avaliar), ("energia-ia", self._analisar), ("energia-avisos", self._avisar)):
            t = threading.Thread(target=alvo, name=nome, daemon=True)
            self._threads.append(t)
            t.start()
        return self

    def _falha(self, erro):
        self.monitor.estado = "falha_registro"
        detalhe = self.monitor.registro.ultimo_erro or {}
        assinatura = (type(erro).__name__, detalhe.get("arquivo"), detalhe.get("operacao"))
        agora = time.monotonic()
        if self._ultima_falha != assinatura or agora - self._ultima_falha_em >= 10:
            arquivo = detalhe.get("arquivo", "arquivo de registros")
            operacao = detalhe.get("operacao", "gravar")
            print(f"[Energia] Falha de registro: {type(erro).__name__} em {arquivo} ({operacao}); "
                  f"errno={detalhe.get('errno')}, winerror={detalhe.get('winerror')}. "
                  "Confira permissões, bloqueio por sincronização/antivírus e outra instância do monitor.")
            self._ultima_falha, self._ultima_falha_em = assinatura, agora

    def _avaliar(self):
        while not self._parar.is_set():
            tipo = self.monitor.config.sensor_tipo
            leitura = self.arduino.ultima_leitura_sensor(tipo)
            try:
                self.monitor.atualizar(leitura.bruto if leitura else None,
                    leitura_instante=leitura.instante if leitura else None,
                    disponivel=self.arduino.conectado and self.arduino.suporta_sensor(tipo))
            except OSError as erro:
                self._falha(erro)
            self._parar.wait(self.monitor.config.intervalo_s)

    def _analisar(self):
        while not self._parar.is_set():
            try:
                pedido = self.monitor.pedidos.get(timeout=0.1)
            except queue.Empty:
                if time.monotonic() >= self._proxima_regravacao:
                    try:
                        self.monitor.regravar_resultados()
                    except OSError as erro:
                        self._falha(erro)
                        self._proxima_regravacao = time.monotonic() + 1
                continue
            try:
                self.monitor.analisar_pedido(pedido)
            except OSError as erro:
                self._falha(erro)
                self._proxima_regravacao = time.monotonic() + 1
            finally:
                self.monitor.pedidos.task_done()

    def _efeito_motor_aviso(self, evento_id):
        """Fábrica de contexto para a fila de áudio; um gesto por aviso, inclusive no fallback."""
        iniciado = False

        def atual():
            return (not self._parar.is_set() and self.arduino.conectado
                    and self.monitor.aviso_atual(evento_id))

        @contextmanager
        def efeito():
            nonlocal iniciado
            cfg = self.monitor.config
            if iniciado or not cfg.motor_aviso or not atual():
                yield
                return
            iniciado = True
            with self.arduino.cabeca_balancando(cfg.motor_balanco_ms,
                    duracao_max_s=cfg.motor_duracao_s, condicao=atual):
                yield

        return efeito

    def _avisar(self):
        while not self._parar.is_set():
            try:
                aviso = self.monitor.avisos.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if not self.monitor.aviso_atual(aviso["evento_id"]):
                    continue
                print(f"[Energia / {aviso['fonte']}] {aviso['mensagem']}")
                efeito = self._efeito_motor_aviso(aviso["evento_id"])
                if self.voz:
                    # Diálogo guiado recebe prioridade de fala; a ocorrência continua
                    # sendo avaliada/registrada e o aviso é revalidado ao sair da janela.
                    while self.voz.em_dialogo and not self._parar.is_set():
                        self._parar.wait(.1)
                    if self._parar.is_set() or not self.monitor.aviso_atual(aviso["evento_id"]):
                        continue
                    self.voz.enfileirar(aviso["mensagem"], olhos="vermelho", efeito=efeito,
                        condicao=lambda eid=aviso["evento_id"]: not self._parar.is_set() and self.monitor.aviso_atual(eid))
                else:
                    with self.arduino.olhos_acesos("vermelho"), efeito():
                        self._parar.wait(max(1, self.monitor.config.motor_duracao_s)
                                         if self.monitor.config.motor_aviso else 1)
            finally:
                self.monitor.avisos.task_done()

    def fechar(self):
        self._parar.set()
        for t in self._threads:
            t.join(timeout=self.monitor.config.ia_timeout_s + 2)
        if any(t.is_alive() for t in self._threads):
            raise RuntimeError("Uma tarefa não encerrou no prazo; checkpoint preservado. Confira o provedor de IA.")
        self.monitor.encerrar()


def executar_monitoramento(arduino, voz=None, segundos=None, pasta=None, microfone=False):
    monitor = MonitorEnergia(registro=RegistroEnergia(pasta)) if pasta is not None else None
    servico = ServicoMonitoramento(arduino, voz, monitor).iniciar()
    fim = time.monotonic() + segundos if segundos is not None else float("inf")
    anterior = None
    tipo = servico.monitor.config.sensor_tipo
    rotulo = "DO (pino 8)" if tipo == "digital" else "ADC (A0)"
    print(f"Sensor configurado: {tipo}. {rotulo}. Sem troca automática de entrada.")
    try:
        if microfone:
            if voz is None:
                raise ValueError("Microfone exige a saída de voz habilitada.")
            from .dialogo_energia import ControladorVozEnergia
            ControladorVozEnergia(servico.monitor, voz).executar(parar=servico._parar, fim=fim)
            return servico.monitor
        while time.monotonic() < fim:
            d = servico.monitor.diagnostico()
            aviso = d.get("aviso", {})
            atual = (d["bruto"], d["estado"], d["ocorrencia"], d["analise"], aviso.get("estado"))
            if atual != anterior:
                print(f"{rotulo}={d['bruto']} | {d['estado']} | Arduino={arduino.conectado} | "
                      f"aviso={aviso.get('estado')} | última análise={d['analise']}")
                anterior = atual
            servico._parar.wait(0.5)
    except KeyboardInterrupt:
        print("Monitoramento encerrado pelo usuário.")
    finally:
        servico.fechar()
    return servico.monitor
