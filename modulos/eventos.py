"""Persistência local. Contrato compartilhado só contém eventos finalizados com custo conhecido."""

import json
import math
import os
import threading
import tempfile
import time
from pathlib import Path
from .config import RAIZ, obter_variavel


def estimar_custo(potencia_kw, duracao_s, tarifa_rs_kwh):
    if potencia_kw is None or tarifa_rs_kwh is None:
        return None
    if not all(math.isfinite(v) and v > 0 for v in (potencia_kw, duracao_s, tarifa_rs_kwh)):
        raise ValueError("Parâmetros de custo devem ser positivos e finitos.")
    return round(potencia_kw * duracao_s / 3600 * tarifa_rs_kwh, 8)


class RegistroEnergia:
    """Um escritor por processo; não execute duas instâncias na mesma pasta."""

    def __init__(self, pasta=None):
        caminho = pasta or obter_variavel("MAKER_DADOS_DIR")
        self.pasta = Path(caminho) if caminho else RAIZ / "dados/maker"
        if not self.pasta.is_absolute():
            self.pasta = RAIZ / self.pasta
        self._trava = threading.RLock()
        self.ultimo_erro = None
        self._assinatura_ativos = None
        self._ids = {nome: {d["id"] for d in self.ler(nome)} for nome in
                     ("eventos.jsonl", "eventos_pendentes.jsonl")}

    def _registrar_erro(self, erro, arquivo, operacao):
        # Não copia str(erro): pode conter dados de outros diretórios/usuários.
        self.ultimo_erro = {"erro_tipo": type(erro).__name__, "arquivo": Path(arquivo).name,
                           "operacao": operacao, "errno": erro.errno,
                           "winerror": getattr(erro, "winerror", None)}

    def _tentar_acesso(self, acao, arquivo, operacao):
        """Retenta só abertura/substituição, nunca repete um append já iniciado."""
        for tentativa in range(4):
            try:
                return acao()
            except PermissionError as erro:
                self._registrar_erro(erro, arquivo, operacao)
                if tentativa == 3:
                    raise
                time.sleep((.02, .05, .1)[tentativa])
            except OSError as erro:
                self._registrar_erro(erro, arquivo, operacao)
                raise

    def ler(self, arquivo):
        caminho = self.pasta / arquivo
        if not caminho.exists():
            return []
        try:
            conteudo = self._tentar_acesso(lambda: caminho.read_text(encoding="utf-8"), arquivo, "ler")
            return [json.loads(l) for l in conteudo.splitlines() if l.strip()]
        except (ValueError, KeyError) as erro:
            raise ValueError(f"Registro inválido em {arquivo}; preserve e revise o arquivo.") from erro

    def gravar(self, arquivo, dados):
        with self._trava:
            self._tentar_acesso(lambda: self.pasta.mkdir(parents=True, exist_ok=True), arquivo, "criar_pasta")
            linha = json.dumps(dados, ensure_ascii=False, allow_nan=False) + "\n"
            caminho = self.pasta / arquivo
            try:
                with self._tentar_acesso(lambda: caminho.open("a", encoding="utf-8"), arquivo, "abrir_append") as saida:
                    saida.write(linha)
                    saida.flush()
                    os.fsync(saida.fileno())
            except OSError as erro:
                self._registrar_erro(erro, arquivo, "gravar_append")
                raise

    def salvar_ativos(self, ativos):
        """Snapshot atômico com duração já observada; monotônico nunca atravessa processos."""
        with self._trava:
            self._tentar_acesso(lambda: self.pasta.mkdir(parents=True, exist_ok=True), "estado_ativo.json", "criar_pasta")
            # Contexto muda a cada tick; checkpoint só precisa mudar em segundos
            # observados ou transições da ocorrência. Reduz substituições no OneDrive.
            assinatura = json.dumps([{k: v for k, v in a.items() if k != "contexto"}
                                     for a in ativos], sort_keys=True, allow_nan=False)
            if assinatura == self._assinatura_ativos:
                return
            temp = None
            destino = self.pasta / "estado_ativo.json"
            operacao = "criar_temporario"
            try:
                fd, nome = self._tentar_acesso(
                    lambda: tempfile.mkstemp(prefix=".estado_ativo-", suffix=".tmp", dir=self.pasta),
                    destino, "criar_temporario")
                temp = Path(nome)
                operacao = "gravar_temporario"
                with os.fdopen(fd, "w", encoding="utf-8") as saida:
                    json.dump(ativos, saida, ensure_ascii=False, allow_nan=False)
                    saida.flush()
                    os.fsync(saida.fileno())
                operacao = "substituir_checkpoint"
                self._tentar_acesso(lambda: temp.replace(destino), destino, operacao)
                self._assinatura_ativos = assinatura
            except OSError as erro:
                self._registrar_erro(erro, destino, operacao)
                raise
            finally:
                if temp is not None:
                    try:
                        temp.unlink(missing_ok=True)
                    except OSError:
                        pass  # mantém erro original e checkpoint anterior

    def carregar_ativos(self):
        caminho = self.pasta / "estado_ativo.json"
        return json.loads(self._tentar_acesso(lambda: caminho.read_text(encoding="utf-8"),
                                            caminho, "ler_checkpoint")) if caminho.exists() else []

    def ler_documento(self, arquivo):
        caminho = self.pasta / arquivo
        if not caminho.exists():
            return None
        try:
            return json.loads(self._tentar_acesso(lambda: caminho.read_text(encoding="utf-8"), arquivo, "ler_configuracao"))
        except ValueError as erro:
            raise ValueError(f"{arquivo} contém JSON inválido; nenhuma configuração salva foi aplicada.") from erro

    def salvar_documento(self, arquivo, dados):
        """Confirmação atômica de configuração; o arquivo anterior sobrevive a falhas."""
        with self._trava:
            self._tentar_acesso(lambda: self.pasta.mkdir(parents=True, exist_ok=True), arquivo, "criar_pasta")
            conteudo = json.dumps(dados, ensure_ascii=False, allow_nan=False)
            destino, temporario = self.pasta / arquivo, None
            try:
                fd, nome = self._tentar_acesso(
                    lambda: tempfile.mkstemp(prefix=".expediente-", suffix=".tmp", dir=self.pasta),
                    arquivo, "criar_configuracao_temporaria")
                temporario = Path(nome)
                with os.fdopen(fd, "w", encoding="utf-8") as saida:
                    saida.write(conteudo)
                    saida.flush()
                    os.fsync(saida.fileno())
                self._tentar_acesso(lambda: temporario.replace(destino), arquivo, "substituir_configuracao")
            finally:
                if temporario is not None:
                    try:
                        temporario.unlink(missing_ok=True)
                    except OSError:
                        pass

    def historico_energia(self, loja_id, equipamento_id):
        """Carregado ao iniciar, antes das tarefas. Consultas por voz usam cache do monitor."""
        analises, avisos = {}, []
        for dado in self.ler("auditoria.jsonl"):
            contexto = dado.get("contexto", {})
            if (dado.get("estado") == "analise" and contexto.get("loja_id") == loja_id
                    and contexto.get("equipamento_id") == equipamento_id):
                analises[dado["evento_id"]] = {"evento_id": dado["evento_id"], "fonte": dado["fonte"],
                    **dado["decisao"], "contexto": contexto}
            if dado.get("estado") == "aviso_solicitado":
                avisos.append(dado)
        eventos = {}
        # Exportados prevalecem sobre o mesmo UUID ainda presente no arquivo de pendências.
        for nome in ("eventos_pendentes.jsonl", "eventos.jsonl"):
            for dado in self.ler(nome):
                if dado.get("loja_id") == loja_id and dado.get("equipamento_id") == equipamento_id:
                    eventos[dado["id"]] = {**dado, "fonte_dados": nome, "ativo": False,
                                          "analise": analises.get(dado["id"])}
        ultimo_aviso = next((a for a in reversed(avisos) if a.get("evento_id") in analises), None)
        return eventos, ultimo_aviso, analises

    def finalizar(self, ocorrencia):
        if not ocorrencia.get("registrar"):
            return
        duracao = max(1, int(ocorrencia["duracao_s"]))
        custo = estimar_custo(ocorrencia.get("potencia_kw"), duracao, ocorrencia.get("tarifa_rs_kwh"))
        evento = {k: ocorrencia[k] for k in ("id", "loja_id", "equipamento_id", "origem", "inicio")}
        evento.update(tipo="luz_acesa_sem_necessidade", duracao_s=duracao, custo_estimado_rs=custo)
        self._evento(evento, "eventos.jsonl" if custo is not None else "eventos_pendentes.jsonl")

    def _evento(self, evento, arquivo):
        with self._trava:
            # Se fsync falhou depois da escrita, a próxima tentativa também é idempotente.
            self._ids[arquivo].update(d["id"] for d in self.ler(arquivo))
            if evento["id"] not in self._ids[arquivo]:
                self.gravar(arquivo, evento)
                self._ids[arquivo].add(evento["id"])

    def exportar_pendentes(self, potencia_kw, tarifa_rs_kwh):
        """Compatibiliza custo ausente sem mudar UUID, duração ou histórico local."""
        if potencia_kw is None or tarifa_rs_kwh is None:
            raise ValueError("Configure potência de iluminação e tarifa antes de exportar.")
        quantidade = 0
        for evento in self.ler("eventos_pendentes.jsonl"):
            if evento["id"] in self._ids["eventos.jsonl"]:
                continue
            custo = estimar_custo(potencia_kw, evento["duracao_s"], tarifa_rs_kwh)
            self._evento({**evento, "custo_estimado_rs": custo}, "eventos.jsonl")
            quantidade += 1
        return quantidade
