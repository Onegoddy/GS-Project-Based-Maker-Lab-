"""Regressões para a montagem física informada: VCC→5V, GND→GND, DO→8."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import testar_arduino
from modulos.arduino import Arduino, LeituraLDR
from modulos.config import BRASILIA, ConfigEnergia
from modulos.eventos import RegistroEnergia
from modulos.monitoramento_energia import MonitorEnergia
from test_energia import DECISAO
from test_sensor_luz import PRONTO, SerialSimulada


class SerialDigitalTest(unittest.TestCase):
    def setUp(self):
        self.t = 0.
        self.arduino = Arduino(ativo=False, sensor_tipo="digital", relogio=lambda: self.t)
        self.arduino.serial = SerialSimulada()
        self.arduino._processar_bytes(PRONTO)
        self.addCleanup(self.arduino.fechar)

    def test_do_real_sem_converter_em_adc_ou_usar_a0(self):
        self.arduino._processar_bytes(b"TEL:LDR:235:500\nLUZ:0\n")
        leitura = self.arduino.obter_leitura()
        self.assertEqual(leitura.bruto, 0)
        self.assertEqual(leitura.tipo, "digital")
        self.assertIsNone(leitura.millis_placa)
        self.assertEqual(self.arduino.leitura_atual().bruto, 235)
        self.assertEqual(self.arduino.ler_luminosidade(), 0)
        self.assertIn("DO no pino 8", self.arduino.descrever_luminosidade())

    def test_adc_continuo_nao_mascara_do_ausente(self):
        self.arduino._processar_bytes(b"TEL:LDR:235:500\n")
        self.assertIsNone(self.arduino.obter_leitura())
        self.assertIsNone(self.arduino.ultima_leitura_valida)

    def test_adc_novo_nao_renova_do_antigo(self):
        self.arduino._processar_bytes(b"LUZ:1\n")
        self.t = 4
        self.arduino._processar_bytes(b"TEL:LDR:235:4000\nLUZ:2\n")
        self.assertIsNone(self.arduino.obter_leitura())
        self.assertEqual(self.arduino.ultima_leitura_valida, 0)
        self.assertEqual(self.arduino.ultima_leitura_sensor().bruto, 1)

    def test_fw_v3_e_v4_suportam_digital(self):
        for pronto in (b"PRONTO v3\n", PRONTO):
            self.arduino._processar_bytes(pronto + b"LUZ:1\n")
            self.assertTrue(self.arduino.suporta_sensor("digital"))
            self.assertEqual(self.arduino.obter_leitura().bruto, 1)

    def test_fw_sem_luz_digital_nao_usa_adc_como_substituto(self):
        self.arduino._processar_bytes(b"PRONTO v4 CAPS=LDR_ANALOGICO\nTEL:LDR:235:500\n")
        self.assertFalse(self.arduino.suporta_sensor())
        self.assertIsNone(self.arduino.obter_leitura())

    def test_leitor_unico_consulta_do_no_v4(self):
        class Placa(SerialSimulada):
            def write(self, valor):
                super().write(valor)
                if valor == b"L": self.fila.put(b"LUZ:1\n")
        placa = Placa(PRONTO + b"TEL:LDR:235:500\n")
        arduino = Arduino("COM_SIMULADA", fabrica_serial=lambda *a, **k: placa, sensor_tipo="digital")
        try:
            fim = time.monotonic() + 1
            while arduino.obter_leitura() is None and time.monotonic() < fim: time.sleep(.01)
            self.assertEqual(arduino.obter_leitura().bruto, 1)
            self.assertIn(b"L", placa.enviados)
            self.assertEqual(placa.leitores, {"arduino-serial"})
        finally:
            arduino.fechar()


class CalibracaoDigitalTest(unittest.TestCase):
    def setUp(self):
        self.arduino = Mock(sensor_tipo="digital")
        self.arduino.suporta_sensor.return_value = True

    def test_aceita_polaridades_medidas_sem_exigir_20_adc(self):
        for referencias in ((1, 0), (0, 1)):
            saida = io.StringIO()
            with self.subTest(referencias=referencias), redirect_stdout(saida), \
                 patch("builtins.input", return_value=""), \
                 patch.object(testar_arduino, "coletar_amostras", side_effect=[[r]*5 for r in referencias]):
                self.assertEqual(testar_arduino.calibrar_sensor(self.arduino), referencias)
            self.assertIn("MAKER_SENSOR_TIPO=digital", saida.getvalue())
            self.assertIn(f"MAKER_REFERENCIA_ESCURO={referencias[0]}", saida.getvalue())

    def test_do_igual_nas_duas_condicoes_nao_aceita_referencias(self):
        saida = io.StringIO()
        with redirect_stdout(saida), patch("builtins.input", return_value=""), \
             patch.object(testar_arduino, "coletar_amostras", side_effect=[[1]*5, [1]*5]):
            with self.assertRaisesRegex(SystemExit, "DO ficou igual"):
                testar_arduino.calibrar_sensor(self.arduino)
        self.assertNotIn("MAKER_REFERENCIA_ESCURO=", saida.getvalue())

    def test_do_oscilando_ou_adc_nao_sao_referencias_digitais(self):
        for valores in ([0, 1, 1, 0], [235]*5):
            with self.subTest(valores=valores), redirect_stdout(io.StringIO()), \
                 patch("builtins.input", return_value=""), \
                 patch.object(testar_arduino, "coletar_amostras", return_value=valores):
                with self.assertRaisesRegex(SystemExit, "DO instável ou inválido"):
                    testar_arduino.calibrar_sensor(self.arduino)

    def test_coleta_descarta_cache_anterior_ao_enter_e_sequencias_repetidas(self):
        t = [0.]
        velha = LeituraLDR(1, -1, datetime.now(timezone.utc), None, 1, "digital")
        nova = LeituraLDR(0, .1, datetime.now(timezone.utc), None, 2, "digital")
        self.arduino.obter_leitura.side_effect = lambda: velha if t[0] == 0 else nova
        def esperar(segundos): t[0] += segundos
        valores = testar_arduino.coletar_amostras(self.arduino, 1, relogio=lambda: t[0], esperar=esperar)
        self.assertEqual(valores, [0])


class MonitorDigitalTest(unittest.TestCase):
    def test_ciclo_temporal_contexto_e_registro_usam_do_sem_adc_inventado(self):
        with tempfile.TemporaryDirectory() as pasta:
            t = [0.]
            base = datetime(2026, 11, 12, 22, tzinfo=BRASILIA)
            cfg = ConfigEnergia(sensor_tipo="digital", referencia_escuro=1, referencia_claro=0,
                                persistencia_s=3, potencia_kw=.08, tarifa_rs_kwh=.95)
            ia = Mock()
            ia.analisar_iluminacao.return_value = DECISAO
            registro = RegistroEnergia(pasta)
            monitor = MonitorEnergia(ia, cfg, registro, relogio=lambda: t[0],
                                    agora=lambda: base + timedelta(seconds=t[0]))
            for n in range(4):
                t[0] = n
                monitor.atualizar(0)
            monitor.analisar_pedido(monitor.pedidos.get_nowait())
            contexto = ia.analisar_iluminacao.call_args.args[0]
            self.assertEqual(contexto["sensor_tipo"], "digital")
            self.assertEqual(contexto["pino"], "8")
            self.assertEqual(contexto["leitura_digital"], 0)
            self.assertIsNone(contexto["leitura_adc"])
            self.assertIsNone(contexto["luminosidade_relativa"])
            self.assertTrue(contexto["ambiente_claro"])
            t[0] = 4; monitor.atualizar(0)
            self.assertEqual(monitor.avisos.qsize(), 1)
            t[0] = 5; monitor.atualizar(1)
            self.assertEqual(monitor.estado, "ambiente_escuro")
            self.assertEqual(registro.ler("eventos.jsonl")[0]["duracao_s"], 5)
            self.assertTrue(all(l["sensor_tipo"] == "digital" and l["bruto"] in (0, 1)
                                for l in registro.ler("leituras.jsonl")))

    def test_adc_em_modo_digital_fica_indisponivel(self):
        with tempfile.TemporaryDirectory() as pasta:
            monitor = MonitorEnergia(Mock(), ConfigEnergia(sensor_tipo="digital"), RegistroEnergia(pasta))
            self.assertEqual(monitor.atualizar(235).estado, "indisponivel")
            self.assertTrue(monitor.pedidos.empty())

    def test_config_rejeita_calibracao_analogica_em_digital_e_tipo_invalido(self):
        for kwargs in ({"sensor_tipo": "digital", "referencia_escuro": 235, "referencia_claro": 900},
                       {"sensor_tipo": "outro"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): ConfigEnergia(**kwargs)

    def test_config_le_selecao_explicita_do_ambiente(self):
        valores = {"MAKER_SENSOR_TIPO": "digital", "MAKER_REFERENCIA_ESCURO": "0", "MAKER_REFERENCIA_CLARO": "1"}
        with patch("modulos.config.obter_variavel", side_effect=lambda nome, padrao=None: valores.get(nome, padrao)):
            cfg = ConfigEnergia.do_ambiente()
        self.assertEqual(cfg.sensor_tipo, "digital")
        self.assertEqual(cfg.maximo_sensor, 1)
        self.assertEqual(cfg.normalizar(1), 1)
