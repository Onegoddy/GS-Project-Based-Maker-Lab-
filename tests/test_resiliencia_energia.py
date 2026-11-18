"""Falhas observadas em execução: JSON HTTP 400, bloqueio de arquivos e TTS online."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import maker_lab
from modulos.config import BRASILIA, ConfigEnergia
from modulos.eventos import RegistroEnergia
from modulos.ia_iluminacao import IAIluminacao, ErroAnaliseIA, diagnosticar_erro_ia
from modulos.monitoramento_energia import MonitorEnergia, ServicoMonitoramento
from modulos.voz import Voz
from test_energia import DECISAO


class PersistenciaResilienteTest(unittest.TestCase):
    def setUp(self):
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        self.registro = RegistroEnergia(pasta.name)

    def test_checkpoint_repeticao_temporaria_nao_perde_estado(self):
        self.registro.salvar_ativos([])
        original = Path.replace
        tentativas = []
        def substituir(caminho, destino):
            tentativas.append(caminho.name)
            if len(tentativas) < 3: raise PermissionError(13, "bloqueio", str(destino))
            return original(caminho, destino)
        ativos = [{"id": "teste", "duracao_s": 2}]
        with patch.object(Path, "replace", substituir), patch("modulos.eventos.time.sleep"):
            self.registro.salvar_ativos(ativos)
        self.assertEqual(len(tentativas), 3)
        self.assertEqual(self.registro.carregar_ativos(), ativos)
        self.assertEqual(list(self.registro.pasta.glob(".estado_ativo-*.tmp")), [])

    def test_bloqueio_permanente_preserva_checkpoint_anterior_e_informa_arquivo(self):
        self.registro.salvar_ativos([])
        with patch.object(Path, "replace", side_effect=PermissionError(13, "bloqueio")) as substituir, \
             patch("modulos.eventos.time.sleep"):
            with self.assertRaises(PermissionError): self.registro.salvar_ativos([{"id": "novo"}])
        self.assertEqual(substituir.call_count, 4)
        self.assertEqual(self.registro.carregar_ativos(), [])
        self.assertEqual(self.registro.ultimo_erro["arquivo"], "estado_ativo.json")
        self.assertEqual(self.registro.ultimo_erro["operacao"], "substituir_checkpoint")
        self.assertEqual(list(self.registro.pasta.glob(".estado_ativo-*.tmp")), [])

    def test_checkpoint_ignora_ticks_de_contexto_sem_transicao(self):
        with patch.object(Path, "replace", autospec=True) as substituir:
            self.registro.salvar_ativos([{"id": "teste", "duracao_s": 3, "contexto": {"agora": "t1"}}])
            self.registro.salvar_ativos([{"id": "teste", "duracao_s": 3, "contexto": {"agora": "t2"}}])
            self.assertEqual(substituir.call_count, 1)
            self.registro.salvar_ativos([{"id": "teste", "duracao_s": 4, "contexto": {"agora": "t3"}}])
            self.assertEqual(substituir.call_count, 2)

    def test_erro_depois_do_append_nao_reexecuta_append(self):
        with patch("modulos.eventos.os.fsync", side_effect=PermissionError(13, "bloqueio")):
            with self.assertRaises(PermissionError): self.registro.gravar("leituras.jsonl", {"bruto": 1})
        self.assertEqual(self.registro.ler("leituras.jsonl"), [{"bruto": 1}])

    def test_abertura_bloqueada_retenta_sem_duplicar_linha(self):
        original = Path.open
        tentativas = []
        def abrir(caminho, *args, **kwargs):
            if caminho.name == "leituras.jsonl" and args and args[0] == "a":
                tentativas.append(1)
                if len(tentativas) < 3: raise PermissionError(13, "bloqueio")
            return original(caminho, *args, **kwargs)
        with patch.object(Path, "open", abrir), patch("modulos.eventos.time.sleep"):
            self.registro.gravar("leituras.jsonl", {"bruto": 0})
        self.assertEqual(len(tentativas), 3)
        self.assertEqual(self.registro.ler("leituras.jsonl"), [{"bruto": 0}])

    def test_resultado_ia_e_regravado_sem_nova_inferencia(self):
        t = [0.]
        base = datetime(2026, 11, 15, 22, tzinfo=BRASILIA)
        ia = Mock()
        ia.analisar_iluminacao.return_value = DECISAO
        monitor = MonitorEnergia(ia, ConfigEnergia(sensor_tipo="digital", referencia_escuro=1,
            referencia_claro=0, persistencia_s=2), self.registro,
            relogio=lambda: t[0], agora=lambda: base + timedelta(seconds=t[0]))
        for n in range(3): t[0] = n; monitor.atualizar(0)
        original = self.registro.gravar
        def gravar(arquivo, dados):
            if dados.get("estado") == "analise": raise PermissionError(13, "bloqueio")
            return original(arquivo, dados)
        with patch.object(self.registro, "gravar", side_effect=gravar):
            with self.assertRaises(PermissionError): monitor.analisar_pedido(monitor.pedidos.get_nowait())
        self.assertEqual(len(monitor._resultados_pendentes), 1)
        monitor.regravar_resultados()
        self.assertEqual(ia.analisar_iluminacao.call_count, 1)
        self.assertEqual(monitor._resultados_pendentes, {})
        self.assertEqual(monitor.ultima_analise["fonte"], "ia")
        monitor.encerrar()

    def test_falha_registro_repetida_tem_limite_de_mensagens(self):
        monitor = Mock()
        monitor.registro = self.registro
        self.registro.ultimo_erro = {"arquivo": "estado_ativo.json", "operacao": "substituir_checkpoint"}
        servico = ServicoMonitoramento(Mock(), monitor=monitor)
        saida = io.StringIO()
        with redirect_stdout(saida):
            servico._falha(PermissionError())
            servico._falha(PermissionError())
        self.assertEqual(saida.getvalue().count("Falha de registro"), 1)
        self.assertIn("estado_ativo.json", saida.getvalue())


class ErrosIATest(unittest.TestCase):
    def test_http_400_json_validate_failed_tem_diagnostico_sem_corpo_secreto(self):
        erro = HTTPError("https://api.groq.com", 400, "segredo", {}, io.BytesIO(json.dumps({
            "error": {"code": "json_validate_failed", "message": "segredo", "failed_generation": "segredo"}}).encode()))
        with patch("modulos.ia_iluminacao.obter_variavel", side_effect=lambda n,p=None: "chave-ficticia" if n == "GROQ_API_KEY" else p), \
             patch("modulos.ia_iluminacao.urlopen", side_effect=erro):
            with self.assertRaises(ErroAnaliseIA) as captura: IAIluminacao().analisar({})
        diagnostico = diagnosticar_erro_ia(captura.exception)
        self.assertEqual(diagnostico["http_status"], 400)
        self.assertEqual(diagnostico["codigo"], "json_validate_failed")
        self.assertNotIn("segredo", json.dumps(diagnostico))
        self.assertNotIn("chave-ficticia", str(captura.exception))

    def test_groq_outro_modelo_mantem_json_object(self):
        resposta = io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(DECISAO)}}]}).encode())
        with patch("modulos.ia_iluminacao.obter_variavel", side_effect=lambda n,p=None: "teste" if n == "GROQ_API_KEY" else p), \
             patch("modulos.ia_iluminacao.urlopen", return_value=resposta) as http:
            IAIluminacao(modelo="llama-3.3-70b-versatile").analisar({})
        corpo = json.loads(http.call_args.args[0].data)
        self.assertEqual(corpo["response_format"], {"type": "json_object"})
        self.assertNotIn("reasoning_effort", corpo)

    def test_diagnostico_consulta_modelos_sem_geracao(self):
        resposta = io.BytesIO(json.dumps({"data": [{"id": "openai/gpt-oss-20b"}]}).encode())
        with patch("modulos.ia_iluminacao.obter_variavel", side_effect=lambda n,p=None: "teste" if n == "GROQ_API_KEY" else p), \
             patch("modulos.ia_iluminacao.urlopen", return_value=resposta) as http:
            resultado = IAIluminacao().diagnosticar_conexao()
        self.assertTrue(resultado["modelo_disponivel"])
        self.assertFalse(resultado["inferencia_testada"])
        self.assertEqual(http.call_args.args[0].get_method(), "GET")
        self.assertIsNone(http.call_args.args[0].data)

    def test_resposta_cortada_tem_codigo_proprio(self):
        resposta = io.BytesIO(json.dumps({"choices": [{"finish_reason": "length", "message": {"content": "{"}}]}).encode())
        with patch("modulos.ia_iluminacao.obter_variavel", side_effect=lambda n,p=None: "teste" if n == "GROQ_API_KEY" else p), \
             patch("modulos.ia_iluminacao.urlopen", return_value=resposta):
            with self.assertRaises(ErroAnaliseIA) as captura: IAIluminacao().analisar({})
        self.assertEqual(captura.exception.codigo, "limite_tokens")


class VozResilienteTest(unittest.TestCase):
    def voz(self, motor="edge"):
        with patch.object(Voz, "_descobrir_voz_portugues", return_value=None):
            voz = Voz(escutar=False, motor=motor)
        self.addCleanup(voz.fechar)
        return voz

    def test_falha_edge_respeita_cooldown_e_usa_offline(self):
        voz = self.voz()
        with patch.object(voz, "_falar_edge", return_value=False) as edge, \
             patch.object(voz, "_falar_pyttsx3") as offline:
            voz.falar("primeira"); voz.falar("segunda")
        self.assertEqual(edge.call_count, 1)
        self.assertEqual(offline.call_count, 2)

    def test_condicao_resolvida_durante_falha_online_nao_cai_em_fala_tardia(self):
        voz = self.voz()
        ativo = [True]
        def falhar(*args, **kwargs): ativo[0] = False; return False
        with patch.object(voz, "_falar_edge", side_effect=falhar), patch.object(voz, "_falar_pyttsx3") as offline:
            fim = voz.enfileirar("aviso", condicao=lambda: ativo[0])
            self.assertTrue(fim.wait(1))
            offline.assert_not_called()

    def test_sintese_terminou_com_condicao_resolvida_nao_reproduz_audio(self):
        voz = self.voz()
        ativo, arquivos = [True], []
        class Comunicar:
            async def save(self, caminho):
                Path(caminho).write_bytes(b"audio-ficticio")
                arquivos.append(caminho)
                ativo[0] = False
        modulo = types.SimpleNamespace(Communicate=lambda *a, **k: Comunicar())
        with patch.dict(sys.modules, {"edge_tts": modulo}), \
             patch.object(voz, "_tocar_mp3") as tocar, patch.object(voz, "_falar_pyttsx3") as offline:
            fim = voz.enfileirar("aviso", condicao=lambda: ativo[0])
            self.assertTrue(fim.wait(2))
            tocar.assert_not_called(); offline.assert_not_called()
        self.assertTrue(arquivos)
        self.assertTrue(all(not Path(p).exists() for p in arquivos))

    def test_motor_offline_explicito_nao_tenta_edge(self):
        voz = self.voz("pyttsx3")
        with patch.object(voz, "_falar_edge") as edge, patch.object(voz, "_falar_pyttsx3") as offline:
            voz.falar("offline")
            edge.assert_not_called(); offline.assert_called_once()


class CLIDiagnosticoTest(unittest.TestCase):
    def test_diagnostico_nao_abre_arduino(self):
        with patch.object(sys, "argv", ["maker_lab.py", "--diagnostico-ia"]), \
             patch("modulos.ia_generativa.IAGenerativa.diagnosticar_conexao", return_value={
                 "provedor": "groq", "conexao": "ok", "modelo_disponivel": True}) as diagnostico, \
             patch("modulos.arduino.Arduino") as arduino:
            maker_lab.main()
        diagnostico.assert_called_once()
        arduino.assert_not_called()

    def test_modo_fisico_aceita_pasta_e_motor_offline(self):
        with patch.object(sys, "argv", ["maker_lab.py", "--voz", "--voz-motor", "pyttsx3", "--pasta", "tmp/ensaio"]), \
             patch("modulos.arduino.Arduino") as arduino, patch("modulos.voz.Voz") as voz, \
             patch.object(maker_lab, "executar_monitoramento") as executar:
            maker_lab.main()
        voz.assert_called_once_with(arduino=arduino.return_value, escutar=False, motor="pyttsx3")
        self.assertEqual(executar.call_args.kwargs["pasta"], Path("tmp/ensaio"))
