"""Regressão do aviso suprimido por interpretação errada de DO ou do expediente."""
from dataclasses import replace
import unittest

from modulos.ia_iluminacao import IAIluminacao, ErroAnaliseIA
import test_expediente as fakes
from test_energia import DECISAO


CANDIDATO = {"criterio_atencao": "claridade_persistente_fora_expediente",
    "persistencia_confirmada": True, "condicao_calibrada": "iluminado",
    "ambiente_claro": True, "loja_aberta": False, "ocorrencia_encerrada": False,
    "qualidade": "valida_e_calibrada"}


class ContratoContextualTest(unittest.TestCase):
    def test_normal_e_insuficiente_nao_anulam_indicio_calibrado_confirmado(self):
        for classe in ("normal", "dados_insuficientes"):
            with self.subTest(classe=classe), self.assertRaises(ErroAnaliseIA) as falha:
                IAIluminacao.conferir_contexto({**DECISAO, "classificacao": classe}, CANDIDATO)
            self.assertEqual(falha.exception.codigo, "classificacao_incompativel")

    def test_suspeita_validada_preserva_justificativa_e_recomendacao_da_ia(self):
        self.assertEqual(IAIluminacao.conferir_contexto(DECISAO, CANDIDATO), DECISAO)

    def test_historico_desatualizado_expediente_e_falta_de_criterio_nao_sao_forcados(self):
        for mudanca in ({"ocorrencia_encerrada": True}, {"qualidade": "desatualizada"},
                        {"loja_aberta": True}, {"persistencia_confirmada": False},
                        {"ambiente_claro": False}, {"criterio_atencao": None}):
            for classe in ("normal", "dados_insuficientes"):
                with self.subTest(mudanca=mudanca, classe=classe):
                    decisao = {**DECISAO, "classificacao": classe}
                    self.assertEqual(IAIluminacao.conferir_contexto(decisao, {**CANDIDATO, **mudanca}), decisao)


class AvisoContextualTest(unittest.TestCase):
    setUp = fakes.ExpedienteTest.setUp
    novo = fakes.ExpedienteTest.novo
    atualizar = fakes.ExpedienteTest.atualizar
    ativar_ocorrencia = fakes.ExpedienteTest.ativar_ocorrencia

    def _reproduzir_classificacao_errada(self, classe):
        self.ia.analisar_iluminacao.return_value = {**DECISAO, "classificacao": classe,
            "justificativa": "A iluminação parece adequada e por isso não há indício fora do horário."}
        eid, _ = self.ativar_ocorrencia()
        analise = self.monitor.ultima_analise
        self.assertEqual(analise["fonte"], "contingencia_deterministica")
        self.assertEqual(analise["falha"]["codigo"], "classificacao_incompativel")
        self.assertEqual(self.monitor.estado, "possivel_desperdicio")
        self.assertEqual(self.monitor.avisos.qsize(), 1)
        for t in range(5, 9):
            self.atualizar(0, t)
        self.assertEqual(self.monitor.avisos.qsize(), 1)
        self.ia.analisar_iluminacao.assert_called_once()
        registro = next(a for a in self.monitor.registro.ler("auditoria.jsonl") if a["estado"] == "analise")
        self.assertEqual(registro["decisao_ia_rejeitada"]["classificacao"], classe)
        self.assertEqual(registro["decisao"]["classificacao"], "possivel_desperdicio")
        self.atualizar(1, 9)
        self.atualizar(1, 10)
        self.assertEqual([e["id"] for e in self.monitor.registro.ler("eventos.jsonl")], [eid])

    def test_ia_normal_incorreta_gera_contingencia_identificada_sem_duplicacao(self):
        self._reproduzir_classificacao_errada("normal")

    def test_ia_pede_adc_no_digital_nao_silencia_aviso(self):
        self._reproduzir_classificacao_errada("dados_insuficientes")

    def test_contexto_explicita_zero_como_claro_na_montagem_atual(self):
        self.ativar_ocorrencia()
        contexto = self.ia.analisar_iluminacao.call_args.args[0]
        self.assertIn("DO=0 corresponde a ambiente iluminado", contexto["interpretacao_sensor"])
        self.assertEqual(contexto["condicao_calibrada"], "iluminado")
        self.assertTrue(contexto["persistencia_confirmada"])
        self.assertIsNone(contexto["leitura_adc"])

    def test_polaridade_oposta_tambem_respeitada(self):
        self.monitor = self.novo(replace(self.cfg, referencia_escuro=0, referencia_claro=1))
        for t in range(4):
            self.atualizar(1, t)
        self.monitor.analisar_pedido(self.monitor.pedidos.get_nowait())
        contexto = self.ia.analisar_iluminacao.call_args.args[0]
        self.assertIn("DO=1 corresponde a ambiente iluminado", contexto["interpretacao_sensor"])
        self.assertEqual(self.monitor.ultima_analise["fonte"], "ia")

    def test_claro_durante_expediente_continua_sem_analise_ou_aviso(self):
        self.base = self.base.replace(hour=12)
        for t in range(8):
            self.atualizar(0, t)
        self.assertEqual(self.monitor.estado, "dentro_do_horario")
        self.assertTrue(self.monitor.avisos.empty())
        self.ia.analisar_iluminacao.assert_not_called()
