"""Regressões do roteador sem inicializar hardware, microfone ou câmera."""
import unittest
from unittest.mock import Mock
from madara import AssistenteMadara


class ComandosTest(unittest.TestCase):
    def setUp(self):
        self.a = AssistenteMadara.__new__(AssistenteMadara)
        self.a.nome, self.a.ativa = "madara", True
        self.a.voz = Mock()
        self.a.utils = Mock()
        self.a.arduino = Mock()
        self.a._perguntar_ia = Mock()
        self.a._comando_energia = Mock()

    def test_perguntas_energia_antes_dos_genericos(self):
        for texto in ("Devo desligar a luz?", "Há quanto tempo a luz está acesa?",
                      "quantas horas de iluminação?", "calcular custo de energia",
                      "como está a iluminação?"):
            self.a.processar_comando(texto)
            self.assertTrue(self.a.ativa)
        self.assertEqual(self.a._comando_energia.call_count, 5)
        self.a.utils.previsao_tempo.assert_not_called()
        self.a.utils.hora_atual.assert_not_called()
        self.a.utils.calcular.assert_not_called()
        self.a._perguntar_ia.assert_not_called()

    def test_comandos_monitoramento(self):
        for texto, esperado in (("qual foi o último alerta?", "ultimo_alerta"),
                                ("mostrar resumo de energia", "resumo"),
                                ("pausar monitoramento", "pausar"),
                                ("retomar monitoramento", "retomar")):
            self.a.processar_comando(texto)
            self.assertEqual(self.a._comando_energia.call_args.args[0], esperado)

    def test_encerramento_explicito(self):
        self.a.processar_comando("encerrar a assistente")
        self.assertFalse(self.a.ativa)

    def test_hora_clima_e_calculo_antigos(self):
        for texto in ("que horas são", "previsão do tempo", "calcular 2 mais 2"):
            self.a.processar_comando(texto)
        self.a.utils.hora_atual.assert_called_once()
        self.a.utils.previsao_tempo.assert_called_once()
        self.a.utils.calcular.assert_called_once()

    def test_wake_word_obrigatoria_com_limite_de_palavra(self):
        self.a.voz.ouvir.side_effect = ["pausar monitoramento", "madaramente pausar monitoramento", "Madara, encerrar a assistente"]
        self.a._laco_principal()
        self.a._comando_energia.assert_not_called()
        self.assertFalse(self.a.ativa)

    def test_sensor_antigo_continua_disponivel(self):
        self.a.processar_comando("testar sensor de luminosidade")
        self.a.arduino.descrever_luminosidade.assert_called_once()
        self.a._comando_energia.assert_not_called()

    def test_acoes_reais_do_monitor_sao_metodos_da_assistente(self):
        # Executa o método concreto; não basta testar só a escolha de intenção.
        monitor = Mock()
        monitor.consultar_ultimo_alerta.return_value = None
        self.a.monitoramento = Mock(monitor=monitor)
        for intencao in ("pausar", "retomar", "ultimo_alerta", "resumo"):
            AssistenteMadara._comando_energia(self.a, intencao, "")
        monitor.pausar.assert_called_once()
        monitor.retomar.assert_called_once()
        monitor.resumo.assert_called_once()
        self.assertEqual(self.a.voz.falar.call_count, 4)

    def test_consulta_energia_executa_metodo_concreto_com_contexto_atual(self):
        monitor = Mock()
        monitor.diagnostico.return_value = {"persistencia_s": 12, "ocorrencia": "evento-teste"}
        monitor.ultima_analise = {"evento_id": "evento-teste", "recomendacao": "Verifique a origem da claridade."}
        monitor.estado = "possivel_desperdicio"
        monitor.descrever.return_value = "Há claridade persistente fora do horário."
        monitor.contexto_energia.return_value = {"loja_aberta": False, "monitor": {
            "pausado": False, "disponivel": True, "calibrado": True,
            "ambiente_claro": True, "persistencia_s": 12}}
        self.a.monitoramento = Mock(monitor=monitor)
        del self.a._comando_energia
        self.a.processar_comando("Há quanto tempo a luz está acesa?")
        frase = self.a.voz.falar.call_args.args[0]
        self.assertIn("12 segundos", frase)
        self.assertIn("não comprova", frase)
        self.a.utils.previsao_tempo.assert_not_called()
