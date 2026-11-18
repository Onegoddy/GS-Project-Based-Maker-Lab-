"""Continuidade durante diálogos/consultas e entrada CLI com microfone explicitamente falso."""
from contextlib import nullcontext, redirect_stderr
from datetime import datetime
import sys
import io
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import maker_lab
from modulos.arduino import LeituraLDR
from modulos.config import ConfigEnergia, BRASILIA
from modulos.dialogo_energia import ControladorVozEnergia
from modulos.eventos import RegistroEnergia
from modulos.monitoramento_energia import MonitorEnergia, ServicoMonitoramento, executar_monitoramento
from modulos.voz import Voz
from test_concorrencia_energia import aguardar
from test_energia import DECISAO
from test_expediente import VozDuble


class FonteDigital:
    conectado = True
    bruto = 0

    def ultima_leitura_sensor(self, tipo):
        return LeituraLDR(self.bruto, time.monotonic(), datetime.now(BRASILIA), None, 1, "digital")

    def suporta_sensor(self, tipo):
        return tipo == "digital"

    def olhos_acesos(self, cor):
        return nullcontext()


class MicrofoneTest(unittest.TestCase):
    def _verificar_continuidade(self, bloquear):
        iniciou, liberar = threading.Event(), threading.Event()
        fonte = FonteDigital()
        ia = Mock()
        ia.analisar_iluminacao.return_value = DECISAO
        voz = VozDuble(["cancelar"])
        voz.enfileirar = Mock()
        with tempfile.TemporaryDirectory() as pasta:
            cfg = ConfigEnergia(sensor_tipo="digital", referencia_escuro=1, referencia_claro=0,
                persistencia_s=.03, intervalo_s=.01, potencia_kw=.08, tarifa_rs_kwh=.95)
            monitor = MonitorEnergia(ia, cfg, RegistroEnergia(pasta),
                agora=lambda: datetime(2026, 11, 16, 22, tzinfo=BRASILIA))
            controle = ControladorVozEnergia(monitor, voz, ia)
            if bloquear == "captura":
                def ouvir(**kwargs):
                    iniciou.set()
                    liberar.wait(3)
                    return "cancelar"
                voz.ouvir = ouvir
                alvo = lambda: controle.processar_fala("Madara, configurar horário de funcionamento")
            else:
                def conversar(*args, **kwargs):
                    iniciou.set()
                    liberar.wait(3)
                    return {"tipo": "informacao_geral", "resposta": "Revise os hábitos de iluminação."}
                ia.responder_energia.side_effect = conversar
                alvo = lambda: controle.processar_fala("Madara, como economizar energia no estoque?")
            trabalhador = threading.Thread(target=alvo)
            servico = ServicoMonitoramento(fonte, voz, monitor)
            try:
                trabalhador.start()
                self.assertTrue(iniciou.wait(1))
                servico.iniciar()
                self.assertTrue(aguardar(lambda: monitor.estado == "possivel_desperdicio"))
                self.assertTrue(trabalhador.is_alive())
                n = len(monitor.registro.ler("leituras.jsonl"))
                fonte.bruto = 1
                self.assertTrue(aguardar(lambda: monitor.estado == "ambiente_escuro"))
                self.assertGreater(len(monitor.registro.ler("leituras.jsonl")), n)
                self.assertEqual(len(monitor.registro.ler("eventos.jsonl")), 1)
                if bloquear == "captura":
                    voz.enfileirar.assert_not_called()
                liberar.set()
                trabalhador.join(2)
                self.assertFalse(trabalhador.is_alive())
                if bloquear == "captura":
                    self.assertTrue(aguardar(lambda: monitor.avisos.unfinished_tasks == 0))
                    voz.enfileirar.assert_not_called()
            finally:
                liberar.set()
                trabalhador.join(2)
                servico.fechar()
            self.assertTrue(all(not t.is_alive() for t in servico._threads))

    def test_serial_avaliacao_eventos_continuam_durante_captura_guiada(self):
        self._verificar_continuidade("captura")

    def test_avaliacao_eventos_continuam_durante_consulta_ia_conversacional(self):
        self._verificar_continuidade("ia")

    def test_reserva_de_dialogo_liberada_mesmo_com_erro(self):
        with patch.object(Voz, "_descobrir_voz_portugues", return_value=None):
            voz = Voz(escutar=False)
        self.addCleanup(voz.fechar)
        with self.assertRaises(RuntimeError):
            with voz.dialogo():
                self.assertTrue(voz.em_dialogo)
                raise RuntimeError("falha simulada")
        self.assertFalse(voz.em_dialogo)

    def test_cli_microfone_ativa_entrada_sem_camera_preservando_saida(self):
        arduino = Mock()
        with patch.object(sys, "argv", ["maker_lab.py", "--voz", "--microfone"]), \
             patch("modulos.arduino.Arduino", return_value=arduino), \
             patch("modulos.voz.Voz") as fabrica, \
             patch.object(maker_lab, "executar_monitoramento") as executar:
            maker_lab.main()
        fabrica.assert_called_once_with(arduino=arduino, escutar=True)
        executar.assert_called_once_with(arduino, voz=fabrica.return_value, segundos=None, microfone=True)
        fabrica.return_value.fechar.assert_called_once()
        arduino.fechar.assert_called_once()

    def test_cli_rejeita_microfone_implicito_ou_simulado_sem_hardware(self):
        for argumentos in (["--microfone"], ["--voz", "--microfone", "--demonstracao"]):
            with redirect_stderr(io.StringIO()), patch.object(sys, "argv", ["maker_lab.py", *argumentos]), \
                 patch("modulos.arduino.Arduino") as arduino, self.assertRaises(SystemExit):
                maker_lab.main()
            arduino.assert_not_called()

    def test_entrada_maker_entrega_monitor_ao_controlador_e_encerra_servico(self):
        servico = Mock()
        servico.monitor.config.sensor_tipo = "digital"
        with patch("modulos.monitoramento_energia.ServicoMonitoramento", return_value=servico), \
             patch("modulos.dialogo_energia.ControladorVozEnergia") as controle:
            servico.iniciar.return_value = servico
            executar_monitoramento(Mock(), voz=Mock(), microfone=True)
        self.assertIs(controle.call_args.args[0], servico.monitor)
        controle.return_value.executar.assert_called_once()
        servico.fechar.assert_called_once()
