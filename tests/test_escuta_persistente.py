"""Sessão de microfone persistente durante silêncio, sem dispositivo ou rede reais."""
from contextlib import redirect_stdout
from datetime import datetime
import io
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from modulos.config import BRASILIA, ConfigEnergia
from modulos.eventos import RegistroEnergia
from modulos.monitoramento_energia import MonitorEnergia, ServicoMonitoramento
from modulos.voz import Voz
from test_concorrencia_energia import aguardar
from test_energia import DECISAO
from test_microfone_energia import FonteDigital


class Silencio(Exception):
    pass


class FalaIncompreensivel(Exception):
    pass


class FalhaReconhecimento(Exception):
    pass


class MicrofoneDuble:
    def __init__(self):
        self.stream = None
        self.aberturas = self.fechamentos = 0

    def __enter__(self):
        self.aberturas += 1
        self.stream = object()
        return self

    def __exit__(self, *args):
        self.fechamentos += 1
        self.stream = None


class EscutaPersistenteTest(unittest.TestCase):
    def setUp(self):
        self.microfones = []
        self.reconhecedor = Mock()
        self.reconhecedor.listen.side_effect = Silencio()
        self.reconhecedor.recognize_google.return_value = "Madara, como está a iluminação?"
        def criar():
            m = MicrofoneDuble()
            self.microfones.append(m)
            return m
        self.sr = SimpleNamespace(Recognizer=Mock(return_value=self.reconhecedor),
            Microphone=Mock(side_effect=criar), WaitTimeoutError=Silencio,
            UnknownValueError=FalaIncompreensivel, RequestError=FalhaReconhecimento)
        with patch.dict(sys.modules, {"speech_recognition": self.sr}), \
             patch.object(Voz, "_descobrir_voz_portugues", return_value=None):
            self.voz = Voz(motor="pyttsx3")
        self.addCleanup(self.voz.fechar)

    def test_silencio_reutiliza_microfone_e_imprime_ouvindo_so_uma_vez(self):
        saida = io.StringIO()
        with redirect_stdout(saida):
            for _ in range(6):
                self.assertEqual(self.voz.ouvir(), "")
        self.sr.Microphone.assert_called_once()
        self.reconhecedor.adjust_for_ambient_noise.assert_called_once()
        self.reconhecedor.recognize_google.assert_not_called()
        self.assertEqual(saida.getvalue().count("🎤 Ouvindo"), 1)
        self.assertEqual(self.microfones[0].fechamentos, 0)
        self.voz.fechar()
        self.assertEqual(self.microfones[0].fechamentos, 1)

    def test_frase_libera_microfone_antes_da_transcricao_e_reabre_sem_recalibrar(self):
        self.voz.ouvir()
        self.reconhecedor.listen.side_effect = [object(), Silencio()]
        def transcrever(*args, **kwargs):
            self.assertIsNone(self.microfones[0].stream)
            self.assertIsNone(self.voz._microfone)
            return "Madara, qual é o expediente?"
        self.reconhecedor.recognize_google.side_effect = transcrever
        self.assertEqual(self.voz.ouvir(), "madara, qual é o expediente?")
        self.voz.ouvir()
        self.assertEqual(len(self.microfones), 2)
        self.assertEqual(self.microfones[0].fechamentos, 1)
        self.assertEqual(self.microfones[1].fechamentos, 0)
        self.reconhecedor.adjust_for_ambient_noise.assert_called_once()

    def test_reproducao_libera_sessao_e_nao_captura_propria_voz(self):
        iniciou, liberar = threading.Event(), threading.Event()
        self.voz.ouvir()
        def falar(*args, **kwargs):
            self.assertIsNone(self.voz._microfone)
            self.assertEqual(self.microfones[0].fechamentos, 1)
            iniciou.set()
            liberar.wait(2)
        with patch.object(self.voz, "_falar_pyttsx3", side_effect=falar):
            fim = self.voz.enfileirar("Aviso de energia.")
            try:
                self.assertTrue(iniciou.wait(1))
                self.assertEqual(self.voz.ouvir(), "")
                self.assertEqual(len(self.microfones), 1)
                self.assertEqual(self.reconhecedor.listen.call_count, 1)
            finally:
                liberar.set()
            self.assertTrue(fim.wait(1))
        self.voz.ouvir()
        self.assertEqual(len(self.microfones), 2)
        self.assertIsNotNone(self.microfones[1].stream)

    def test_aviso_cancelado_na_fila_preserva_sessao_em_silencio(self):
        self.voz.ouvir()
        fim = self.voz.enfileirar("Aviso resolvido.", condicao=lambda: False)
        self.assertTrue(fim.wait(1))
        self.voz.ouvir()
        self.assertEqual(len(self.microfones), 1)
        self.assertEqual(self.microfones[0].fechamentos, 0)

    def test_falha_de_captura_libera_e_permite_nova_calibracao(self):
        self.reconhecedor.listen.side_effect = [OSError("dispositivo dublê"), Silencio()]
        with self.assertRaises(OSError):
            self.voz.ouvir()
        self.assertEqual(self.microfones[0].fechamentos, 1)
        self.voz.ouvir()
        self.assertEqual(len(self.microfones), 2)
        self.assertEqual(self.reconhecedor.adjust_for_ambient_noise.call_count, 2)

    def test_abertura_sem_stream_nao_e_tratada_como_microfone_ativo(self):
        quebrado = Mock()
        quebrado.__enter__ = Mock(return_value=SimpleNamespace(stream=None))
        self.sr.Microphone.side_effect = None
        self.sr.Microphone.return_value = quebrado
        with self.assertRaisesRegex(RuntimeError, "fluxo de áudio"):
            self.voz.ouvir()
        self.assertIsNone(self.voz._microfone)
        self.reconhecedor.listen.assert_not_called()

    def test_keyboard_interrupt_libera_microfone(self):
        self.reconhecedor.listen.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.voz.ouvir()
        self.assertEqual(self.microfones[0].fechamentos, 1)
        self.assertIsNone(self.voz._microfone)

    def test_fechar_nao_reabre_dispositivo_nem_duplica_fechamento(self):
        self.voz.ouvir()
        self.voz.fechar()
        self.voz.fechar()
        self.assertEqual(self.voz.ouvir(), "")
        self.assertEqual(self.microfones[0].fechamentos, 1)
        self.assertEqual(len(self.microfones), 1)
        self.assertFalse(self.voz._thread.is_alive())

    def test_encerramento_durante_captura_nao_deixa_sessao_aberta(self):
        iniciou, liberar = threading.Event(), threading.Event()
        def capturar(*args, **kwargs):
            iniciou.set()
            liberar.wait(2)
            raise Silencio()
        self.reconhecedor.listen.side_effect = capturar
        capturando = threading.Thread(target=self.voz.ouvir)
        fechando = threading.Thread(target=self.voz.fechar)
        capturando.start()
        try:
            self.assertTrue(iniciou.wait(1))
            fechando.start()
            self.assertTrue(aguardar(lambda: self.voz._fechada))
        finally:
            liberar.set()
            capturando.join(2)
            if fechando.ident is not None:
                fechando.join(2)
        self.assertFalse(capturando.is_alive())
        self.assertFalse(fechando.is_alive())
        self.assertEqual(self.microfones[0].fechamentos, 1)
        self.reconhecedor.recognize_google.assert_not_called()

    def test_monitor_avalia_e_registra_durante_escuta_em_silencio(self):
        iniciou, liberar = threading.Event(), threading.Event()
        def capturar(*args, **kwargs):
            iniciou.set()
            liberar.wait(2)
            raise Silencio()
        self.reconhecedor.listen.side_effect = capturar
        fonte = FonteDigital()
        ia = Mock()
        ia.analisar_iluminacao.return_value = DECISAO
        with tempfile.TemporaryDirectory() as pasta:
            cfg = ConfigEnergia(sensor_tipo="digital", referencia_escuro=1, referencia_claro=0,
                persistencia_s=.03, intervalo_s=.01, motor_aviso=False,
                potencia_kw=.08, tarifa_rs_kwh=.95)
            monitor = MonitorEnergia(ia, cfg, RegistroEnergia(pasta),
                agora=lambda: datetime(2026, 11, 16, 23, tzinfo=BRASILIA))
            capturando = threading.Thread(target=self.voz.ouvir)
            servico = ServicoMonitoramento(fonte, monitor=monitor)
            capturando.start()
            try:
                self.assertTrue(iniciou.wait(1))
                servico.iniciar()
                self.assertTrue(aguardar(lambda: monitor.estado == "possivel_desperdicio"))
                self.assertTrue(capturando.is_alive())
                n = len(monitor.registro.ler("leituras.jsonl"))
                fonte.bruto = 1
                self.assertTrue(aguardar(lambda: monitor.estado == "ambiente_escuro"))
                self.assertTrue(aguardar(lambda: len(monitor.registro.ler("leituras.jsonl")) > n))
                self.assertEqual(len(monitor.registro.ler("eventos.jsonl")), 1)
            finally:
                liberar.set()
                capturando.join(2)
                servico.fechar()
            self.assertEqual(len(self.microfones), 1)
