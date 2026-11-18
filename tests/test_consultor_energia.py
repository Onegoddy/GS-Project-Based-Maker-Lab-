"""Consultor profissional e consultas determinísticas com dublês, sem chamadas pagas."""
import io
import json
from unittest.mock import Mock, patch
import unittest

from modulos.consultor_energia import (ConsultorEnergia, INSTRUCAO_CONVERSA_ENERGIA,
    dentro_escopo, intervencao_eletrica)
from modulos.dialogo_energia import ControladorVozEnergia
from modulos.ia_generativa import IAGenerativa
from modulos.ia_iluminacao import INSTRUCAO
from modulos.intencoes import intencao_energia
import test_expediente as fakes


RESPOSTA = {"tipo": "informacao_geral", "resposta": "Revise os hábitos de iluminação e mantenha apenas a luz necessária às tarefas."}


class TransporteConsultorTest(unittest.TestCase):
    def test_groq_prompt_proprio_contexto_filtrado_timeout_e_schema(self):
        contexto = {"monitor": {"disponivel": False}, "ambiente": "estoque"}
        retorno = io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(RESPOSTA)}}]}).encode())
        with patch("modulos.ia_iluminacao.obter_variavel", side_effect=lambda n,p=None: "chave-duble" if n == "GROQ_API_KEY" else p), \
             patch("modulos.ia_iluminacao.urlopen", return_value=retorno) as http:
            resposta = IAGenerativa().responder_energia("como economizar energia no estoque?", contexto, timeout_s=2)
        self.assertEqual(resposta, RESPOSTA)
        corpo = json.loads(http.call_args.args[0].data)
        self.assertEqual(corpo["messages"][0]["content"], INSTRUCAO_CONVERSA_ENERGIA)
        self.assertNotEqual(corpo["messages"][0]["content"], INSTRUCAO)
        self.assertNotIn("ninja", corpo["messages"][0]["content"])
        self.assertEqual(json.loads(corpo["messages"][1]["content"])["contexto"], contexto)
        self.assertEqual(corpo["response_format"]["json_schema"]["name"], "conversa_energia")
        self.assertEqual(http.call_args.kwargs["timeout"], 2)

    def test_ollama_mesma_operacao_profissional(self):
        retorno = io.BytesIO(json.dumps({"response": json.dumps(RESPOSTA)}).encode())
        with patch("modulos.ia_iluminacao.urlopen", return_value=retorno) as http:
            resposta = IAGenerativa(provedor="ollama").responder_energia("Dicas de iluminação", {"tipo": "informacao_geral"}, timeout_s=3)
        self.assertEqual(resposta, RESPOSTA)
        corpo = json.loads(http.call_args.args[0].data)
        self.assertEqual(corpo["system"], INSTRUCAO_CONVERSA_ENERGIA)
        self.assertEqual(corpo["format"], "json")
        self.assertEqual(http.call_args.kwargs["timeout"], 3)

    def test_fora_escopo_e_intervencao_nao_chamam_rede(self):
        with patch("modulos.ia_iluminacao.urlopen") as http:
            c = ConsultorEnergia()
            for pergunta in ("quem ganhou o campeonato?", "conte uma piada sobre energia"):
                self.assertEqual(c.responder(pergunta, {})["tipo"], "fora_escopo")
            for pergunta in ("como emendar os fios da instalação elétrica?", "como instalar tomadas?"):
                self.assertEqual(c.responder(pergunta, {})["tipo"], "orientacao_profissional")
        http.assert_not_called()

    def test_validacao_rejeita_inventados_numeros_formato_e_alegacoes(self):
        for valor in ({}, [], {**RESPOSTA, "extra": "x"}, {**RESPOSTA, "tipo": "qualquer"},
            {**RESPOSTA, "resposta": "Custo R$ 100"}, {**RESPOSTA, "resposta": "Economia de 20%"},
            {**RESPOSTA, "resposta": "A lâmpada está ligada."},
            {**RESPOSTA, "resposta": "O estoque está vazio."}, {**RESPOSTA, "resposta": "x" * 601}):
            with self.subTest(valor=valor), self.assertRaises(ValueError):
                ConsultorEnergia.validar(valor, {})

    def test_observacao_sem_dados_informa_indisponibilidade(self):
        r = ConsultorEnergia.validar({"tipo": "observacao_sensor", "resposta": "O ambiente está claro."},
                                    {"monitor": {"disponivel": False}})
        self.assertEqual(r["tipo"], "dados_insuficientes")
        self.assertIn("Não há leitura", r["resposta"])

    def test_ressalva_sobre_limites_nao_e_confundida_com_afirmacao(self):
        r = ConsultorEnergia.validar({"tipo": "informacao_geral", "resposta":
            "O LDR não comprova que a lâmpada está ligada. A estimativa não é economia comprovada."}, {})
        self.assertIn("não comprova", r["resposta"])

    def test_json_invalido_falha_sem_retornar_texto_do_provedor(self):
        with patch("modulos.ia_iluminacao.IAIluminacao._cabecalhos", return_value={}), \
             patch("modulos.ia_iluminacao.IAIluminacao._http_json", return_value={"choices": [{"message": {"content": "invalido"}}]}):
            with self.assertRaisesRegex(ValueError, "não passou na validação"):
                ConsultorEnergia().responder("como economizar energia?", {})


class InterfaceConsultorTest(unittest.TestCase):
    # Usa só o preparo das dependências, sem herdar/executar os testes de expediente.
    setUp = fakes.ExpedienteTest.setUp
    novo = fakes.ExpedienteTest.novo
    atualizar = fakes.ExpedienteTest.atualizar
    ativar_ocorrencia = fakes.ExpedienteTest.ativar_ocorrencia

    def controle(self):
        self.voz = fakes.VozDuble()
        self.ia.responder_energia.return_value = RESPOSTA
        return ControladorVozEnergia(self.monitor, self.voz, self.ia)

    def test_dicas_recebem_contexto_atual_digital_sem_historico_indiscriminado(self):
        self.atualizar(0, 0)
        c = self.controle()
        c.processar_fala("Madara, como posso economizar energia no estoque?")
        args = self.ia.responder_energia.call_args
        contexto = args.args[1]
        self.assertEqual(contexto["monitor"]["bruto"], 0)
        self.assertEqual(contexto["monitor"]["sensor_tipo"], "digital")
        self.assertEqual(contexto["loja_nome"], self.monitor.config.loja_nome)
        self.assertFalse(contexto["loja_aberta"])
        self.assertNotIn("ultimo_alerta", contexto)
        self.assertNotIn("parametros", contexto)
        self.assertIn("Informação geral", self.voz.frases[-1])

    def test_perguntas_sobre_o_ldr_consultam_conceito_em_vez_do_estado_atual(self):
        self.atualizar(1, 0)
        controle = self.controle()
        resposta = {"tipo": "informacao_geral", "resposta":
            "O LDR é um sensor cuja resistência varia com a luz. Neste projeto, ajuda a observar a iluminação."}
        self.ia.responder_energia.return_value = resposta
        for pergunta in ("o que é o LDR?", "o que é um LDR?",
                         "para que serve o sensor de luminosidade?", "como funciona um LDR?"):
            with self.subTest(pergunta=pergunta):
                self.ia.responder_energia.reset_mock()
                controle.processar_fala("Madara, " + pergunta)
                self.ia.responder_energia.assert_called_once()
                self.assertIn("resistência varia com a luz", self.voz.frases[-1])
                self.assertNotIn("condição escura", self.voz.frases[-1])

    def test_pergunta_geral_recebe_so_limites(self):
        c = self.controle()
        c.processar_fala("Madara, explique tarifas de energia")
        self.assertEqual(set(self.ia.responder_energia.call_args.args[1]), {"tipo", "limites"})

    def test_consultas_locais_nao_chamam_chatbot_ou_analise(self):
        c = self.controle()
        for fala in ("qual é o horário de funcionamento configurado?", "como está a iluminação?",
                     "qual foi o último alerta?", "quanto custou essa ocorrência?", "qual é a tarifa cadastrada?",
                     "qual é o consumo em kwh?", "mostrar resumo de energia"):
            c.processar_fala("Madara, " + fala)
        self.ia.responder_energia.assert_not_called()
        self.ia.analisar_iluminacao.assert_not_called()
        self.assertTrue(any("não há leitura" in f for f in self.voz.frases))
        self.assertTrue(any("07:00" in f and "21:00" in f for f in self.voz.frases))

    def test_dados_antigos_nao_provam_claridade_atual(self):
        self.atualizar(0, 0)
        self.tempo = 10
        c = self.controle()
        c.processar_fala("Madara, como está a iluminação?")
        self.assertIn("indisponível", self.voz.frases[-1])
        c.processar_fala("Madara, dicas de energia no estoque")
        ctx = self.ia.responder_energia.call_args.args[1]
        self.assertFalse(ctx["monitor"]["disponivel"])
        self.assertIsNone(ctx["monitor"]["bruto"])

    def test_retomada_aguarda_avaliacao_e_pausa_nao_inventa_estado(self):
        self.atualizar(0, 0)
        self.monitor.pausar()
        self.atualizar(1, 1)
        self.assertIsNone(self.monitor.contexto_energia()["monitor"]["ambiente_claro"])
        self.monitor.retomar()
        c = self.controle()
        c.processar_fala("Madara, como está a iluminação?")
        self.assertIn("indisponível", self.voz.frases[-1])
        self.atualizar(1, 2)
        c.processar_fala("Madara, como está a iluminação?")
        self.assertIn("condição escura", self.voz.frases[-1])

    def test_sem_escopo_e_instalacao_recusam_antes_da_ia(self):
        c = self.controle()
        c.processar_fala("Madara, quem ganhou o futebol?")
        self.assertIn("fora do meu escopo", self.voz.frases[-1])
        c.processar_fala("Madara, como instalar uma tomada elétrica?")
        self.assertIn("eletricista qualificado", self.voz.frases[-1])
        self.ia.responder_energia.assert_not_called()

    def test_custo_deterministico_atual_e_historico_apos_reinicio(self):
        self.ativar_ocorrencia()
        c = self.controle()
        c.processar_fala("Madara, quanto custou essa ocorrência?")
        self.assertIn("R$ 0,00008444", self.voz.frases[-1])
        self.assertIn("parcial", self.voz.frases[-1])
        self.atualizar(1, 5)
        self.monitor = self.novo()
        c = self.controle()
        c.processar_fala("Madara, quanto custou essa ocorrência?")
        self.assertIn("R$ 0,00010556", self.voz.frases[-1])
        self.assertNotIn("parcial", self.voz.frases[-1])
        c.processar_fala("Madara, explique o último alerta")
        self.assertIn("Justificativa registrada", self.voz.frases[-1])
        self.assertIn("histórico", self.voz.frases[-1])
        self.ia.responder_energia.assert_not_called()

    def test_custo_desconhecido_nao_vira_zero(self):
        self.monitor.config.potencia_kw = None
        self.ativar_ocorrencia()
        c = self.controle()
        c.processar_fala("Madara, quanto custou essa ocorrência?")
        self.assertIn("indisponível", self.voz.frases[-1])
        self.assertIn("não é custo zero", self.voz.frases[-1])

    def test_custo_do_ultimo_alerta_nao_troca_por_ocorrencia_sem_aviso(self):
        primeiro, _ = self.ativar_ocorrencia()
        self.atualizar(1, 5)
        for t in range(6, 9):
            self.atualizar(0, t)
        self.monitor.analisar_pedido(self.monitor.pedidos.get_nowait())
        self.atualizar(0, 9)
        self.assertNotEqual(self.monitor._ativa, primeiro)
        c = self.controle()
        c.processar_fala("Madara, quanto custou o último alerta?")
        self.assertIn("5 segundos", self.voz.frases[-1])
        self.assertNotIn("parcial", self.voz.frases[-1])

    def test_explicacao_da_contingencia_nao_finge_analise_ia(self):
        self.ia.analisar_iluminacao.side_effect = TimeoutError()
        self.ativar_ocorrencia()
        c = self.controle()
        c.processar_fala("Madara, explique o último alerta")
        self.assertIn("contingência determinística", self.voz.frases[-1])

    def test_consultor_invalido_ou_indisponivel_nao_muda_monitor(self):
        self.atualizar(0, 0)
        c = self.controle()
        estado = self.monitor.estado
        for resposta in (TimeoutError(), {**RESPOSTA, "resposta": "Sua economia é de 100 reais."}):
            self.ia.responder_energia.side_effect = resposta if isinstance(resposta, Exception) else None
            self.ia.responder_energia.return_value = resposta
            c.processar_fala("Madara, dicas de energia no estoque")
            self.assertIn("consultas locais", self.voz.frases[-1])
            self.assertEqual(self.monitor.estado, estado)

    def test_nome_obrigatorio_fora_do_dialogo_e_encerramento_explicito(self):
        c = self.controle()
        self.assertFalse(c.processar_fala("pausar monitoramento"))
        self.assertFalse(c.processar_fala("madaramente pausar monitoramento"))
        self.assertFalse(self.monitor.pausado)
        c.processar_fala("Madara, devo desligar a luz?")
        self.assertTrue(c.ativa)
        c.processar_fala("Madara, encerrar a assistente")
        self.assertFalse(c.ativa)

    def test_prioridades_do_roteador(self):
        exemplos = {"configurar horário de funcionamento": "configurar_expediente",
            "a loja abre às sete da manhã e fecha às nove da noite": "configurar_expediente",
            "qual é o horário de funcionamento configurado?": "expediente",
            "Há quanto tempo a luz está acesa?": "consulta", "Devo desligar a luz?": "consulta",
            "calcular custo de energia": "custo", "quanto custou o último alerta?": "custo",
            "como posso economizar energia no estoque?": "conversar",
            "explique o último alerta": "ultimo_alerta", "pausar monitoramento": "pausar",
            "retomar monitoramento": "retomar", "previsão do tempo": None,
            "que horas são?": None, "calcular 2 mais 2": None}
        for texto, esperado in exemplos.items():
            with self.subTest(texto=texto):
                self.assertEqual(intencao_energia(texto), esperado)
