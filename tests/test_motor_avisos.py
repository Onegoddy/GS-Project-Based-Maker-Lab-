"""Cabeça nos avisos com serial, áudio e IA dublês; nenhum motor real é acionado."""
from contextlib import contextmanager, nullcontext
from datetime import datetime
import queue
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from modulos.arduino import Arduino
from modulos.config import ConfigEnergia, BRASILIA
from modulos.eventos import RegistroEnergia
from modulos.monitoramento_energia import MonitorEnergia, ServicoMonitoramento
from modulos.voz import Voz
from test_concorrencia_energia import aguardar
from test_energia import DECISAO
from test_microfone_energia import FonteDigital
from test_sensor_luz import SerialSimulada


class SerialMotor(SerialSimulada):
    def __init__(self):
        super().__init__()
        self.girou, self.parou = threading.Event(), threading.Event()

    def write(self, comando):
        super().write(comando)
        if comando == b"G":
            self.girou.set()
        if comando == b"P":
            self.parou.set()


class ArduinoMotorTest(unittest.TestCase):
    def setUp(self):
        self.arduino = Arduino(ativo=False, sensor_tipo="digital")
        self.serial = self.arduino.serial = SerialMotor()
        self.addCleanup(self.arduino.fechar)

    def test_limite_para_motor_mesmo_com_contexto_ainda_aberto(self):
        with self.arduino.cabeca_balancando(30, duracao_max_s=.06):
            self.assertTrue(self.serial.girou.wait(.5))
            self.assertTrue(self.serial.parou.wait(.8))
            self.assertEqual(self.serial.enviados[-1], b"P")
            self.assertTrue(aguardar(lambda: not any(t.name == "arduino-cabeca" for t in threading.enumerate())))
        self.assertEqual(self.serial.enviados, [b"G", b"P"])

    def test_resolucao_interrompe_movimento_antes_do_limite(self):
        ativa = threading.Event()
        ativa.set()
        with self.arduino.cabeca_balancando(30, duracao_max_s=2, condicao=ativa.is_set):
            self.assertTrue(self.serial.girou.wait(.5))
            ativa.clear()
            self.assertTrue(self.serial.parou.wait(.8))
        self.assertEqual(self.serial.enviados[-1], b"P")

    def test_uso_legado_sem_limite_e_excecao_ainda_param_no_final(self):
        with self.assertRaises(RuntimeError):
            with self.arduino.cabeca_balancando(30):
                self.assertTrue(aguardar(lambda: b"PG" in self.serial.enviados))
                raise RuntimeError("interrupção simulada da fala")
        self.assertEqual(self.serial.enviados[-1], b"P")
        self.assertFalse(any(t.name == "arduino-cabeca" for t in threading.enumerate()))


class FonteComMotor(FonteDigital):
    def __init__(self):
        self.controle = Arduino(ativo=False, sensor_tipo="digital")
        self.serial_motor = self.controle.serial = SerialMotor()
        self.movimentos = []

    @contextmanager
    def cabeca_balancando(self, balanco_ms, **kwargs):
        self.movimentos.append((balanco_ms, kwargs))
        with self.controle.cabeca_balancando(balanco_ms, **kwargs):
            yield


class MotorAvisoTest(unittest.TestCase):
    def setUp(self):
        self.fonte = FonteComMotor()
        self.addCleanup(self.fonte.controle.fechar)
        self.monitor = Mock()
        self.monitor.config = ConfigEnergia(motor_duracao_s=.06)
        self.monitor.aviso_atual.return_value = True
        self.monitor.avisos = queue.Queue()
        self.servico = ServicoMonitoramento(self.fonte, monitor=self.monitor)

    def nova_voz(self):
        with patch.object(Voz, "_descobrir_voz_portugues", return_value=None):
            voz = Voz(escutar=False, motor="edge")
        self.addCleanup(voz.fechar)
        return voz

    def test_configuracao_limites_e_desativacao_explicita(self):
        self.assertTrue(ConfigEnergia().motor_aviso)
        self.assertEqual(ConfigEnergia().motor_balanco_ms, 30)
        self.assertEqual(ConfigEnergia().motor_duracao_s, 2)
        for dados in ({"motor_balanco_ms": 9}, {"motor_balanco_ms": 61},
                      {"motor_duracao_s": 0}, {"motor_duracao_s": 6}, {"motor_duracao_s": float("nan")}):
            with self.subTest(dados=dados), self.assertRaises(ValueError):
                ConfigEnergia(**dados)
        valores = {"MAKER_MOTOR_AVISO": "false", "MAKER_MOTOR_BALANCO_MS": "25", "MAKER_MOTOR_DURACAO_S": "1.5"}
        with patch("modulos.config.obter_variavel", side_effect=lambda n,p=None: valores.get(n,p)):
            cfg = ConfigEnergia.do_ambiente()
        self.assertFalse(cfg.motor_aviso)
        self.assertEqual((cfg.motor_balanco_ms, cfg.motor_duracao_s), (25, 1.5))

    def test_gesto_unico_mesmo_com_fallback_da_voz(self):
        voz = self.nova_voz()
        efeito = self.servico._efeito_motor_aviso("ocorrencia")
        def edge(texto, efeito, olhos, **kwargs):
            with voz._durante_o_audio(efeito, olhos):
                self.assertTrue(self.fonte.serial_motor.girou.wait(.5))
            return False  # falha de reprodução após início do primeiro gesto
        def offline(texto, efeito, olhos):
            with voz._durante_o_audio(efeito, olhos):
                pass
        with patch.object(voz, "_falar_edge", side_effect=edge), \
             patch.object(voz, "_falar_pyttsx3", side_effect=offline):
            voz.falar("Aviso de teste.", efeito=efeito, olhos="vermelho")
        self.assertEqual(len(self.fonte.movimentos), 1)
        balanco, opcoes = self.fonte.movimentos[0]
        self.assertEqual((balanco, opcoes["duracao_max_s"]), (30, .06))
        self.assertEqual(self.fonte.serial_motor.enviados[-1], b"P")

    def test_aviso_cancelado_na_fila_nao_move_motor(self):
        voz = self.nova_voz()
        self.monitor.aviso_atual.return_value = False
        evento = voz.enfileirar("Cancelado", efeito=self.servico._efeito_motor_aviso("encerrada"),
                               condicao=lambda: self.monitor.aviso_atual("encerrada"))
        self.assertTrue(evento.wait(1))
        self.assertEqual(self.fonte.movimentos, [])

    def test_desativado_ou_servico_encerrado_nao_move(self):
        self.monitor.config.motor_aviso = False
        with self.servico._efeito_motor_aviso("a")():
            pass
        self.monitor.config.motor_aviso = True
        self.servico._parar.set()
        with self.servico._efeito_motor_aviso("b")():
            pass
        self.assertEqual(self.fonte.movimentos, [])

    def test_sem_voz_motor_acompanha_aviso_e_para_ao_encerrar(self):
        aviso = {"evento_id": "sem-voz", "fonte": "contingencia_deterministica", "mensagem": "Verifique a claridade."}
        self.monitor.avisos.put(aviso)
        t = threading.Thread(target=self.servico._avisar)
        self.servico._threads = [t]
        t.start()
        try:
            self.assertTrue(self.fonte.serial_motor.girou.wait(1))
        finally:
            self.servico.fechar()
        self.assertEqual(len(self.fonte.movimentos), 1)
        self.assertEqual(self.fonte.serial_motor.enviados[-1], b"P")
        self.assertFalse(t.is_alive())

    def test_coleta_e_resolucao_continuam_durante_fala_com_movimento(self):
        falou, liberar = threading.Event(), threading.Event()
        voz = self.nova_voz()
        voz.arduino = self.fonte
        ia = Mock()
        ia.analisar_iluminacao.return_value = DECISAO
        with tempfile.TemporaryDirectory() as pasta:
            cfg = ConfigEnergia(sensor_tipo="digital", referencia_escuro=1, referencia_claro=0,
                persistencia_s=.03, intervalo_s=.01, motor_duracao_s=.06,
                potencia_kw=.08, tarifa_rs_kwh=.95)
            monitor = MonitorEnergia(ia, cfg, RegistroEnergia(pasta),
                agora=lambda: datetime(2026, 11, 16, 23, tzinfo=BRASILIA))
            servico = ServicoMonitoramento(self.fonte, voz, monitor)
            def falar(texto, efeito=None, olhos="ambos", **kwargs):
                with voz._durante_o_audio(efeito, olhos):
                    falou.set()
                    liberar.wait(2)
                return True
            with patch.object(voz, "_falar_edge", side_effect=falar):
                servico.iniciar()
                try:
                    self.assertTrue(falou.wait(1))
                    self.assertTrue(self.fonte.serial_motor.girou.wait(.5))
                    self.assertTrue(self.fonte.serial_motor.parou.wait(.8))
                    self.assertFalse(liberar.is_set())  # motor já parou; áudio continua
                    n = len(monitor.registro.ler("leituras.jsonl"))
                    self.fonte.bruto = 1
                    self.assertTrue(aguardar(lambda: monitor.estado == "ambiente_escuro"))
                    self.assertTrue(aguardar(lambda: len(monitor.registro.ler("leituras.jsonl")) > n))
                    self.assertEqual(len(monitor.registro.ler("eventos.jsonl")), 1)
                finally:
                    liberar.set()
                    servico.fechar()
                    voz.fechar()
            self.assertEqual(len(self.fonte.movimentos), 1)
