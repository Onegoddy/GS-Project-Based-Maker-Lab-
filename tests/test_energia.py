"""Lógica temporal e persistência com relógio, sensor e IA simulados."""
from datetime import datetime, timedelta
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import UUID
from modulos.energia import BRASILIA, ConfigEnergia, MonitorEnergia, RegistroEnergia
from modulos.eventos import estimar_custo

DECISAO = {"classificacao": "possivel_desperdicio", "justificativa": "Claridade fora do horário.",
           "recomendacao": "Verifique a origem da claridade.", "mensagem_falada": "Confira a necessidade da iluminação."}

class EnergiaTest(unittest.TestCase):
    def setUp(self):
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        self.registro = RegistroEnergia(pasta.name)
        self.t = 0.
        self.base = datetime(2026, 11, 12, 22, tzinfo=BRASILIA)
        self.ia = Mock()
        self.ia.analisar_iluminacao.return_value = DECISAO
        self.config = ConfigEnergia(referencia_escuro=100, referencia_claro=900,
            persistencia_s=3, intervalo_ia_s=1, pausa_alerta_s=10, potencia_kw=.08, tarifa_rs_kwh=.95)
        self.monitor = self.novo()

    def novo(self):
        return MonitorEnergia(self.ia, self.config, self.registro, relogio=lambda: self.t,
                              agora=lambda: self.base + timedelta(seconds=self.t))

    def passo(self, bruto=900, t=None, hora=None, **kwargs):
        if t is not None: self.t = t
        return self.monitor.atualizar(bruto, agora=hora or self.base + timedelta(seconds=self.t), **kwargs)

    def persistir(self):
        for t in range(4): self.passo(t=t)
        self.assertEqual(self.monitor.pedidos.qsize(), 1)

    def analisar(self):
        self.monitor.analisar_pedido(self.monitor.pedidos.get_nowait())

    def test_claro_no_expediente_nao_consulta_ia(self):
        for t in range(10): self.passo(t=t, hora=self.base.replace(hour=12))
        self.assertEqual(self.monitor.estado, "dentro_do_horario")
        self.assertTrue(self.monitor.pedidos.empty())

    def test_escuro_nao_consulta_ia(self):
        for t in range(10): self.passo(100, t)
        self.assertEqual(self.monitor.estado, "ambiente_escuro")
        self.assertTrue(self.monitor.pedidos.empty())

    def test_persistencia_e_contexto_sensor_na_ia(self):
        self.persistir()
        self.ia.analisar_iluminacao.assert_not_called()  # avaliar nunca espera IA
        self.analisar()
        contexto = self.ia.analisar_iluminacao.call_args.args[0]
        for campo in ("loja", "ambiente", "leitura_adc", "referencia_escuro", "referencia_claro",
                      "persistencia_s", "loja_aberta", "qualidade", "idade_leitura_s"):
            self.assertIn(campo, contexto)
        self.assertEqual(contexto["presenca"], "nao_medida")
        self.assertFalse(contexto["loja_aberta"])
        self.passo(t=4)
        self.assertEqual(self.monitor.avisos.qsize(), 1)

    def test_evento_final_so_na_resolucao_uuid_estavel_sem_duplicata(self):
        self.persistir(); self.analisar()
        eid = self.monitor._ativa
        UUID(eid)
        for t in range(4, 12): self.passo(t=t)
        self.assertEqual(self.registro.ler("eventos.jsonl"), [])
        self.assertEqual(self.monitor.avisos.qsize(), 1)
        self.assertEqual(self.monitor._ativa, eid)
        self.passo(100, 12)
        self.passo(100, 13)
        evento = self.registro.ler("eventos.jsonl")[0]
        self.assertEqual(evento["id"], eid)
        self.assertEqual(evento["duracao_s"], 12)
        self.assertEqual(evento["origem"], "maker")
        self.assertEqual(set(evento), {"id", "loja_id", "equipamento_id", "tipo", "origem", "inicio", "duracao_s", "custo_estimado_rs"})
        self.assertIsNotNone(datetime.fromisoformat(evento["inicio"]).utcoffset())
        self.assertAlmostEqual(evento["custo_estimado_rs"], .08 * 12/3600 * .95, places=8)
        self.assertEqual(len(self.registro.ler("eventos.jsonl")), 1)

    def test_oscilacao_breve_nao_cria_ocorrencia(self):
        self.passo(t=0); self.passo(t=1); self.passo(100, 2)
        self.assertTrue(self.monitor.pedidos.empty())
        self.assertEqual(self.monitor.total_ocorrencias, 0)

    def test_histerese_preserva_estado_na_faixa_e_resolve_abaixo(self):
        self.passo(t=0)
        self.passo(580, 1)  # 0.60: conserva claro
        self.assertEqual(self.monitor.estado, "observando")
        self.passo(480, 2)  # 0.475: abaixo de 0.50
        self.assertEqual(self.monitor.estado, "ambiente_escuro")
        self.passo(580, 3)
        self.assertEqual(self.monitor.estado, "ambiente_escuro")

    def test_adc_invertido(self):
        self.config.referencia_escuro, self.config.referencia_claro = 900, 100
        self.assertEqual(self.passo(900, 0).estado, "ambiente_escuro")
        self.assertEqual(self.passo(100, 1).estado, "observando")

    def test_leitura_desatualizada_nunca_normal_e_duracao_sem_lacuna(self):
        self.persistir(); self.analisar(); self.passo(t=4)
        self.passo(900, 8, leitura_instante=4)
        self.assertEqual(self.monitor.estado, "indisponivel")
        self.assertEqual(self.registro.ler("eventos.jsonl")[0]["duracao_s"], 4)
        self.passo(900, 9)
        self.assertEqual(self.monitor.estado, "observando")

    def test_cache_nao_renova_instante_da_leitura(self):
        self.passo(t=0)
        self.passo(t=2, leitura_instante=0)
        self.assertEqual(self.monitor.ultimo_valido, 0)
        self.assertEqual(len(self.registro.ler("leituras.jsonl")), 1)
        self.assertEqual(self.passo(t=4, leitura_instante=0).estado, "indisponivel")

    def test_gap_entre_amostras_interrompe_periodo(self):
        self.passo(t=0)
        self.passo(t=10)
        self.assertEqual(self.monitor.estado, "observando")
        self.assertTrue(self.monitor.pedidos.empty())

    def test_invalidas_nao_sao_normal(self):
        for bruto in (None, True, -1, 1024, "900", 9.2):
            with self.subTest(bruto=bruto):
                self.assertEqual(self.passo(bruto).estado, "indisponivel")
        self.assertEqual(self.passo(900, disponivel=False).estado, "indisponivel")

    def test_ia_offline_fallback_rotulado_sem_vazar_erro(self):
        self.ia.analisar_iluminacao.side_effect = TimeoutError("segredo-ficticio")
        self.persistir(); self.analisar(); self.passo(t=4)
        self.assertEqual(self.monitor.ultima_analise["fonte"], "contingencia_deterministica")
        self.assertEqual(self.monitor.avisos.qsize(), 1)
        self.assertNotIn("segredo-ficticio", (self.registro.pasta / "auditoria.jsonl").read_text())
        self.passo(100, 5)
        self.assertEqual(len(self.registro.ler("eventos.jsonl")), 1)

    def test_ia_invalida_recebe_contingencia(self):
        self.ia.analisar_iluminacao.return_value = {"classificacao": "certeza"}
        self.persistir(); self.analisar()
        self.assertEqual(self.monitor.ultima_analise["fonte"], "contingencia_deterministica")

    def test_ia_historica_normal_e_dados_insuficientes_nao_geram_evento(self):
        for classe in ("normal", "dados_insuficientes"):
            with self.subTest(classe=classe):
                self.monitor = self.novo()
                self.ia.analisar_iluminacao.return_value = {**DECISAO, "classificacao": classe}
                self.persistir(); self.passo(100, 4); self.analisar(); self.passo(100, 5)
                self.assertTrue(self.monitor.avisos.empty())
        self.assertEqual(self.registro.ler("eventos.jsonl"), [])

    def test_resolucao_durante_ia_registra_sem_aviso_tardio(self):
        self.persistir(); self.passo(100, 4); self.analisar()
        self.assertEqual(len(self.registro.ler("eventos.jsonl")), 1)
        self.assertTrue(self.monitor.avisos.empty())

    def test_reinicio_recupera_checkpoint_e_deduplica(self):
        self.persistir(); self.analisar(); self.passo(t=4)
        eid = self.monitor._ativa
        self.monitor = self.novo()
        self.assertEqual(self.registro.ler("eventos.jsonl")[0]["id"], eid)
        self.monitor = self.novo()
        self.assertEqual(len(self.registro.ler("eventos.jsonl")), 1)

    def test_pausa_coleta_continua_retomada_reinicia_persistencia(self):
        self.persistir(); self.analisar(); self.monitor.pausar()
        self.passo(900, 4)
        self.assertEqual(self.monitor.estado, "pausado")
        self.assertEqual(len(self.registro.ler("leituras.jsonl")), 5)
        self.monitor.retomar(); self.passo(t=5)
        self.assertEqual(self.monitor.estado, "observando")

    def test_cooldown_global_entre_avisos_nao_perde_evento(self):
        self.persistir(); self.analisar(); self.passo(t=4); self.passo(100, 5)
        for t in range(6, 10): self.passo(t=t)
        self.analisar(); self.passo(t=10)
        self.assertEqual(self.monitor.avisos.qsize(), 1)
        for t in range(11, 15): self.passo(t=t)
        self.assertEqual(self.monitor.avisos.qsize(), 2)
        self.passo(100, 15)
        self.assertEqual(len(self.registro.ler("eventos.jsonl")), 2)

    def test_simulacao_e_custo_desconhecido_exportacao_idempotente(self):
        self.monitor.simulado = True
        self.config.potencia_kw = self.config.tarifa_rs_kwh = None
        self.persistir(); self.analisar(); self.passo(100, 4)
        self.assertEqual(self.registro.ler("eventos.jsonl"), [])
        evento = self.registro.ler("eventos_pendentes.jsonl")[0]
        self.assertIsNone(evento["custo_estimado_rs"])
        self.assertEqual(evento["origem"], "simulador")
        self.assertEqual(self.registro.exportar_pendentes(.08, .95), 1)
        self.assertEqual(self.registro.exportar_pendentes(.08, .95), 0)
        self.assertEqual(self.registro.ler("eventos.jsonl")[0]["id"], evento["id"])
        with self.assertRaises(ValueError): self.registro.exportar_pendentes(None, None)

    def test_erro_gravacao_visivel_sem_aviso(self):
        self.passo(t=0)
        with patch.object(self.registro, "gravar", side_effect=OSError("disco cheio")):
            with self.assertRaises(OSError): self.passo(t=1)
        self.assertTrue(self.monitor.avisos.empty())

    def test_horario_noturno_e_fechamento_exclusivo(self):
        cfg = ConfigEnergia(abertura="22:00", fechamento="06:00")
        for h in (22, 23, 0, 5): self.assertTrue(cfg.loja_aberta(self.base.replace(hour=h)))
        for h in (6, 12, 21): self.assertFalse(cfg.loja_aberta(self.base.replace(hour=h)))

    def test_abertura_encerra_episodio(self):
        self.persistir(); self.analisar()
        self.passo(t=4, hora=self.base.replace(hour=7))
        self.assertEqual(self.monitor.estado, "dentro_do_horario")
        self.assertEqual(len(self.registro.ler("eventos.jsonl")), 1)

    def test_duracao_monotonica_independe_relogio_civil(self):
        self.passo(t=0)
        for t in range(1, 4): self.passo(t=t, hora=self.base + timedelta(days=1))
        self.analisar(); self.passo(100, 4)
        self.assertEqual(self.registro.ler("eventos.jsonl")[0]["duracao_s"], 4)

    def test_sem_calibracao_fica_indisponivel_para_inferencia(self):
        self.config.referencia_escuro = self.config.referencia_claro = None
        self.assertEqual(self.passo(t=0).estado, "nao_calibrado")
        self.assertTrue(self.monitor.pedidos.empty())

    def test_ocorrencia_resolvida_durante_intervalo_ia_nao_some(self):
        self.config.intervalo_ia_s = 15
        self.persistir(); self.analisar(); self.passo(100, 4)
        for t in range(5, 9): self.passo(t=t)
        self.assertTrue(self.monitor.pedidos.empty())
        self.passo(100, 9)
        for t in range(10, 19): self.passo(100, t)
        self.assertEqual(self.monitor.pedidos.qsize(), 1)
        self.analisar()
        contexto = self.ia.analisar_iluminacao.call_args.args[0]
        self.assertTrue(contexto["ocorrencia_encerrada"])
        self.assertEqual(contexto["qualidade"], "historica")
        self.assertEqual(len(self.registro.ler("eventos.jsonl")), 2)

    def test_ia_que_esperou_na_fila_recebe_contexto_atual_da_ocorrencia(self):
        self.persistir()
        for t in range(4, 9): self.passo(680, t)
        self.analisar()
        contexto = self.ia.analisar_iluminacao.call_args.args[0]
        self.assertEqual(contexto["leitura_adc"], 680)
        self.assertEqual(contexto["persistencia_s"], 8)
        self.assertEqual(contexto["idade_leitura_s"], 0)
        self.assertEqual(contexto["qualidade"], "valida_e_calibrada")

    def test_fsync_falha_apos_escrita_nao_duplica_na_tentativa(self):
        self.persistir(); self.analisar()
        original = self.registro.gravar
        def falhar_depois(arquivo, dados):
            original(arquivo, dados)
            if arquivo == "eventos.jsonl": raise OSError("fsync simulado")
        with patch.object(self.registro, "gravar", side_effect=falhar_depois):
            with self.assertRaises(OSError): self.passo(100, 4)
        self.passo(100, 5)
        self.assertEqual(len(self.registro.ler("eventos.jsonl")), 1)

    def test_config_invalidas_e_estimativa_sem_defaults(self):
        for kwargs in ({"referencia_escuro": 10}, {"referencia_escuro": 10, "referencia_claro": 10},
                       {"referencia_escuro": 100, "referencia_claro": 900, "sentido": "decrescente"},
                       {"histerese": float("nan")}, {"ia_timeout_s": 31}, {"persistencia_s": 0},
                       {"abertura": "08:00", "fechamento": "08:00"}, {"potencia_kw": float("nan")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): ConfigEnergia(**kwargs)
        self.assertIsNone(estimar_custo(None, 5, .95))
        self.assertIsNone(ConfigEnergia().potencia_kw)
        with self.assertRaises(ValueError): self.config.loja_aberta(datetime(2026, 1, 1))
