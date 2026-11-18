"""Coletores independentes de IA, captura e reprodução, sem dispositivos reais."""
from contextlib import nullcontext
from datetime import datetime, timezone
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from modulos.arduino import LeituraLDR
from modulos.config import BRASILIA, ConfigEnergia
from modulos.eventos import RegistroEnergia
from modulos.monitoramento_energia import MonitorEnergia, ServicoMonitoramento
from modulos.voz import Voz
from test_energia import DECISAO


def aguardar(condicao, limite=2):
    fim = time.monotonic() + limite
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(.005)
    return False


class ConcorrenciaTest(unittest.TestCase):
    def test_avaliacao_e_resolucao_continuam_durante_ia_bloqueada(self):
        iniciou, liberar = threading.Event(), threading.Event()
        class Fonte:
            conectado = sensor_analogico_disponivel = True
            bruto = 900
            @property
            def ultima_leitura(self):
                return LeituraLDR(self.bruto, time.monotonic(), datetime.now(timezone.utc), 500, 1)
            def ultima_leitura_sensor(self, tipo): return self.ultima_leitura
            def suporta_sensor(self, tipo): return tipo == "analogico"
            def olhos_acesos(self, cor): return nullcontext()
        def analisar(*args, **kwargs):
            iniciou.set()
            liberar.wait(2)
            return DECISAO
        fonte = Fonte()
        ia = Mock()
        ia.analisar_iluminacao.side_effect = analisar
        with tempfile.TemporaryDirectory() as pasta:
            cfg = ConfigEnergia(referencia_escuro=100, referencia_claro=900,
                persistencia_s=.03, intervalo_s=.01, potencia_kw=.08, tarifa_rs_kwh=.95)
            monitor = MonitorEnergia(ia, cfg, RegistroEnergia(pasta),
                agora=lambda: datetime(2026, 11, 12, 22, tzinfo=BRASILIA))
            servico = ServicoMonitoramento(fonte, monitor=monitor).iniciar()
            try:
                self.assertTrue(iniciou.wait(1))
                n = len(monitor.registro.ler("leituras.jsonl"))
                fonte.bruto = 100
                self.assertTrue(aguardar(lambda: monitor.estado == "ambiente_escuro"))
                self.assertGreater(len(monitor.registro.ler("leituras.jsonl")), n)
                liberar.set()
                self.assertTrue(aguardar(lambda: bool(monitor.registro.ler("eventos.jsonl"))))
                self.assertTrue(monitor.avisos.empty())
            finally:
                liberar.set()
                servico.fechar()
            self.assertTrue(all(not t.is_alive() for t in servico._threads))

    def test_fila_unica_serializa_falas_e_suspende_escuta(self):
        iniciou, liberar = threading.Event(), threading.Event()
        with patch.object(Voz, "_descobrir_voz_portugues", return_value=None):
            voz = Voz(escutar=False)
        self.addCleanup(voz.fechar)
        ordem = []
        def falar(texto, *args):
            ordem.append(texto)
            if texto == "primeira":
                iniciou.set(); liberar.wait(2)
            return True
        with patch.object(voz, "_falar_edge", side_effect=falar), \
             patch.object(voz, "_iniciar_reconhecimento"), \
             patch.object(voz, "_ouvir_sem_reproducao") as ouvir:
            fim1 = voz.enfileirar("primeira")
            self.assertTrue(iniciou.wait(1))
            fim2 = voz.enfileirar("segunda")
            self.assertEqual(voz.ouvir(), "")
            ouvir.assert_not_called()
            self.assertFalse(fim2.is_set())
            liberar.set()
            self.assertTrue(fim1.wait(1)); self.assertTrue(fim2.wait(1))
            self.assertEqual(ordem, ["primeira", "segunda"])
        voz.fechar()
        self.assertFalse(voz._thread.is_alive())

    def test_aviso_resolvido_na_fila_nao_e_falado(self):
        with patch.object(Voz, "_descobrir_voz_portugues", return_value=None):
            voz = Voz(escutar=False)
        self.addCleanup(voz.fechar)
        with patch.object(voz, "_falar_edge") as falar:
            fim = voz.enfileirar("resolvido", condicao=lambda: False)
            self.assertTrue(fim.wait(1))
            falar.assert_not_called()
