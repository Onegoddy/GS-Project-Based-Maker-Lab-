"""Ressalvas legítimas da IA e avisos aguardando o intervalo global."""
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from modulos.ia_iluminacao import IAIluminacao, ErroAnaliseIA
from modulos.ia_generativa import IAGenerativa
import test_energia as fakes


class ValidacaoSemanticaTest(unittest.TestCase):
    def test_ressalvas_negacoes_e_verificacao_nao_sao_afirmacoes(self):
        for frase in (
            "Não se pode afirmar que a iluminação está acesa, apenas que há claridade persistente.",
            "Não é possível afirmar que a lâmpada está ligada.",
            "O LDR não comprova que a luz está ligada.",
            "Não há consumo medido e não é economia comprovada.",
            "Não sabemos se o estoque está vazio.",
            "Verifique se a iluminação no estoque permanece acesa.",
            "Não desligue equipamentos sem avaliar a necessidade.",
        ):
            with self.subTest(frase=frase):
                dados = {**fakes.DECISAO, "justificativa": frase}
                self.assertEqual(IAIluminacao.validar(dados), dados)

    def test_ressalva_nao_libera_outra_afirmacao_ou_ordem(self):
        for frase, codigo in (
            ("Não se pode afirmar que a iluminação está acesa. O estoque está vazio.", "afirmacao_nao_sustentada"),
            ("O LDR não comprova que a luz está ligada. A luz está ligada.", "afirmacao_nao_sustentada"),
            ("Não desligue sem verificar. Desligue a iluminação agora.", "orientacao_nao_validada"),
            ("Se a luz for interna, desligue o equipamento.", "orientacao_nao_validada"),
            ("Há consumo medido.", "afirmacao_nao_sustentada"),
        ):
            with self.subTest(frase=frase), self.assertRaises(ErroAnaliseIA) as falha:
                IAIluminacao.validar({**fakes.DECISAO, "recomendacao": frase})
            self.assertEqual(falha.exception.codigo, codigo)

    def test_transporte_preserva_codigo_e_campo_sem_corpo_privado(self):
        casos = (
            ({**fakes.DECISAO, "justificativa": "x" * 601}, "campo_invalido", "justificativa"),
            ({**fakes.DECISAO, "recomendacao": "Desligue a luz."}, "orientacao_nao_validada", "recomendacao"),
            ({**fakes.DECISAO, "mensagem_falada": "O estoque está vazio."}, "afirmacao_nao_sustentada", "mensagem_falada"),
            ({**fakes.DECISAO, "justificativa": "linha\nlinha"}, "caracteres_invalidos", "justificativa"),
        )
        for dados, codigo, campo in casos:
            envelope = {"choices": [{"message": {"content": json.dumps(dados)}}]}
            with self.subTest(codigo=codigo), patch.object(IAIluminacao, "_cabecalhos", return_value={}), \
                 patch.object(IAIluminacao, "_http_json", return_value=envelope), \
                 self.assertRaises(ErroAnaliseIA) as falha:
                IAIluminacao().analisar({})
            self.assertEqual(falha.exception.codigo, codigo)
            self.assertIn(campo, falha.exception.orientacao)
            self.assertNotIn(dados[campo], falha.exception.orientacao)

    def test_resposta_com_ressalva_e_recomendacao_segura_passam_no_transporte(self):
        dados = {**fakes.DECISAO, "justificativa":
            "A condição iluminada persiste fora do expediente. Não se pode afirmar que a iluminação está acesa. Não há consumo medido.",
            "recomendacao": "Verifique manualmente a origem da claridade e a necessidade da iluminação."}
        envelope = {"choices": [{"message": {"content": json.dumps(dados)}}]}
        with patch.object(IAIluminacao, "_cabecalhos", return_value={}), \
             patch.object(IAIluminacao, "_http_json", return_value=envelope):
            self.assertEqual(IAIluminacao().analisar({}), dados)


class IntervaloAvisoTest(unittest.TestCase):
    setUp = fakes.EnergiaTest.setUp
    novo = fakes.EnergiaTest.novo
    passo = fakes.EnergiaTest.passo
    persistir = fakes.EnergiaTest.persistir
    analisar = fakes.EnergiaTest.analisar

    def segundo_evento(self):
        self.persistir()
        self.analisar()
        self.passo(t=4)  # primeiro aviso; próximo somente em t=14
        self.passo(100, 5)
        for t in range(6, 10):
            self.passo(t=t)
        self.analisar()

    def test_primeira_ocorrencia_pronta_sem_intervalo_anterior(self):
        self.persistir()
        self.analisar()
        self.assertEqual(self.monitor.diagnostico()["aviso"], {"estado": "pronto_para_aviso", "restante_s": 0})

    def test_adiamento_explica_espera_uma_vez_e_libera_no_limite(self):
        self.segundo_evento()
        saida = io.StringIO()
        with redirect_stdout(saida):
            for t in range(10, 14):
                self.passo(t=t)
        self.assertEqual(saida.getvalue().count("aguardando intervalo mínimo"), 1)
        self.assertEqual(self.monitor.avisos.qsize(), 1)
        self.assertEqual(self.monitor.diagnostico()["aviso"], {"estado": "aguardando_intervalo", "restante_s": 1})
        self.assertIn("faltam 1 segundos", self.monitor.resumo())
        adiados = [r for r in self.registro.ler("auditoria.jsonl") if r["estado"] == "aviso_adiado_intervalo"]
        self.assertEqual(len(adiados), 1)
        self.assertEqual(adiados[0]["restante_s"], 4)
        self.assertIn("-03:00", adiados[0]["previsao_em"])
        self.passo(t=14)
        self.assertEqual(self.monitor.avisos.qsize(), 2)
        self.assertEqual(self.monitor.diagnostico()["aviso"]["estado"], "solicitado")
        self.passo(t=15)
        self.assertEqual(self.monitor.avisos.qsize(), 2)

    def test_encerra_antes_do_limite_sem_aviso_tardio(self):
        self.segundo_evento()
        self.passo(t=10)
        self.passo(100, 11)
        for t in range(12, 16):
            self.passo(100, t)
        self.assertEqual(self.monitor.avisos.qsize(), 1)
        self.assertEqual(self.monitor.diagnostico()["aviso"]["estado"], "sem_ocorrencia")
        self.assertEqual(len(self.registro.ler("eventos.jsonl")), 2)


class RevisaoAnaliseTest(unittest.TestCase):
    def envelope(self, dados):
        return {"choices": [{"message": {"content": json.dumps(dados)}}]}

    def test_revisa_uma_vez_preserva_contexto_e_orcamento(self):
        invalida = {**fakes.DECISAO, "recomendacao": "Desligue a luz."}
        contexto = {"leitura_digital": 0, "ambiente_claro": True}
        operacao = IAIluminacao("groq", modelo="openai/gpt-oss-20b", timeout_s=3)
        tempos, respostas = [], iter([self.envelope(invalida), self.envelope(fakes.DECISAO)])
        def responder(pedido):
            tempos.append(operacao.timeout_s)
            return next(respostas)
        with patch.object(operacao, "_cabecalhos", return_value={}), \
             patch.object(operacao, "_http_json", side_effect=responder) as http, \
             patch("modulos.ia_iluminacao.time.monotonic", side_effect=[10., 11.]):
            self.assertEqual(operacao.analisar(contexto), fakes.DECISAO)
        self.assertEqual(tempos, [3, 2])
        self.assertEqual(operacao.timeout_s, 3)
        self.assertEqual(operacao.ultima_execucao["tentativas"], 2)
        self.assertTrue(operacao.ultima_execucao["validada"])
        self.assertEqual(operacao.ultima_execucao["correcoes"][0]["codigo"], "orientacao_nao_validada")
        corpo = json.loads(http.call_args.args[0].data)
        self.assertEqual(json.loads(corpo["messages"][1]["content"]), contexto)
        self.assertEqual(json.loads(corpo["messages"][2]["content"]), invalida)
        self.assertIn("Campo recomendacao", corpo["messages"][3]["content"])

    def test_segunda_invalida_falha_sem_terceira_requisicao(self):
        invalida = {**fakes.DECISAO, "mensagem_falada": "A luz está ligada."}
        operacao = IAIluminacao()
        with patch.object(operacao, "_cabecalhos", return_value={}), \
             patch.object(operacao, "_http_json", return_value=self.envelope(invalida)) as http, \
             self.assertRaises(ErroAnaliseIA) as falha:
            operacao.analisar({})
        self.assertEqual(falha.exception.codigo, "afirmacao_nao_sustentada")
        self.assertEqual(http.call_count, 2)
        self.assertFalse(operacao.ultima_execucao["validada"])

    def test_sem_tempo_para_revisar_nao_inicia_outra_chamada(self):
        operacao = IAIluminacao(timeout_s=3)
        invalida = {**fakes.DECISAO, "recomendacao": "Desligue a luz."}
        with patch.object(operacao, "_cabecalhos", return_value={}), \
             patch.object(operacao, "_http_json", return_value=self.envelope(invalida)) as http, \
             patch("modulos.ia_iluminacao.time.monotonic", side_effect=[10., 14.]), \
             self.assertRaises(ErroAnaliseIA) as falha:
            operacao.analisar({})
        self.assertEqual(falha.exception.codigo, "timeout")
        http.assert_called_once()
        self.assertEqual(operacao.timeout_s, 3)

    def test_limite_de_uso_nao_dispara_revisao(self):
        operacao = IAIluminacao()
        with patch.object(operacao, "_cabecalhos", return_value={}), \
             patch.object(operacao, "_http_json", side_effect=ErroAnaliseIA("http_429", "Limite de uso", 429)) as http, \
             self.assertRaises(ErroAnaliseIA):
            operacao.analisar({})
        http.assert_called_once()
        self.assertEqual(operacao.ultima_execucao["correcoes"], [])

    def test_ollama_revisa_com_contexto_e_json(self):
        operacao = IAIluminacao("ollama")
        invalida = {**fakes.DECISAO, "recomendacao": "Apague a luz."}
        with patch.object(operacao, "_http_json", side_effect=[
            {"response": json.dumps(invalida)}, {"response": json.dumps(fakes.DECISAO)}]) as http:
            self.assertEqual(operacao.analisar({"leitura_digital": 0}), fakes.DECISAO)
        corpo = json.loads(http.call_args.args[0].data)
        self.assertEqual(corpo["format"], "json")
        self.assertEqual(json.loads(corpo["prompt"])["contexto"]["leitura_digital"], 0)
        self.assertEqual(http.call_count, 2)


class AuditoriaRevisaoTest(unittest.TestCase):
    setUp = fakes.EnergiaTest.setUp
    novo = fakes.EnergiaTest.novo
    passo = fakes.EnergiaTest.passo
    persistir = fakes.EnergiaTest.persistir
    analisar = fakes.EnergiaTest.analisar

    def test_monitor_audita_revisao_validada_e_usa_ia_sem_repetir_aviso(self):
        self.monitor.ia = IAGenerativa()
        invalida = {**fakes.DECISAO, "recomendacao": "Desligue a luz."}
        envelopes = [{"choices": [{"message": {"content": json.dumps(d)}}]} for d in (invalida, fakes.DECISAO)]
        with patch.object(IAIluminacao, "_cabecalhos", return_value={}), \
             patch.object(IAIluminacao, "_http_json", side_effect=envelopes) as http:
            self.persistir()
            self.analisar()
            self.passo(t=4)
            self.passo(t=5)
        self.assertEqual(http.call_count, 2)
        self.assertEqual(self.monitor.avisos.qsize(), 1)
        registro = next(r for r in self.registro.ler("auditoria.jsonl") if r["estado"] == "analise")
        self.assertEqual(registro["fonte"], "ia")
        self.assertEqual(registro["execucao_ia"]["tentativas"], 2)
        self.assertEqual(registro["execucao_ia"]["correcoes"][0]["codigo"], "orientacao_nao_validada")
