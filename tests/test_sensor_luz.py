"""Framing e leitor único sem porta serial real."""
import importlib
import queue
import threading
import time
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch
from modulos.arduino import Arduino

PRONTO = b"PRONTO v4 CAPS=LEDS_SEPARADOS,LDR_ANALOGICO,LUZ_DIGITAL\n"

class SerialSimulada:
    def __init__(self, dados=PRONTO):
        self.is_open = True
        self.fila = queue.Queue()
        self.fila.put(dados)
        self.enviados = []
        self.leitores = set()
        self.falha = False
    @property
    def in_waiting(self): return 1
    def read(self, tamanho):
        self.leitores.add(threading.current_thread().name)
        if self.falha: raise OSError("USB simulado")
        try: return self.fila.get(timeout=.01)
        except queue.Empty: return b""
    def write(self, valor): self.enviados.append(valor)
    def close(self): self.is_open = False

class SensorLuzTest(unittest.TestCase):
    def setUp(self):
        self.tempo = 0.
        self.arduino = Arduino(ativo=False, relogio=lambda: self.tempo, sensor_tipo="analogico")
        self.addCleanup(self.arduino.fechar)

    def test_handshake_preserva_leds_novas_versoes(self):
        for linha, leds, analogico in ((b"PRONTO", False, False), (b"PRONTO v2", True, False),
              (b"PRONTO v3", True, False), (PRONTO.strip(), True, True),
              (b"PRONTO v5 CAPS=LEDS_SEPARADOS,LDR_ANALOGICO", True, True)):
            self.arduino._processar_linha(linha)
            self.assertEqual(self.arduino.leds_separados, leds)
            self.assertEqual(self.arduino.sensor_analogico_disponivel, analogico)

    def test_fragmentos_ruido_invalidas_e_overflow_nao_corrompem(self):
        self.arduino._processar_bytes(PRONTO)
        for bloco in (b"inicio\nTEL:L", b"DR:712:", b"500\r", b"\n"):
            self.arduino._processar_bytes(bloco)
        leitura = self.arduino.ultima_leitura
        self.assertEqual(leitura.bruto, 712)
        self.assertEqual(leitura.instante, 0)
        self.assertIsNotNone(leitura.recebido_em.tzinfo)
        self.tempo = 1
        self.arduino._processar_bytes(b"TEL:LDR:1024:1000\nTEL:LDR:x:1000\n")
        self.arduino._processar_bytes(b"x"*200 + b"TEL:LDR:800:1000\n")
        self.assertEqual(self.arduino.ultima_leitura, leitura)
        self.arduino._processar_bytes(b"\xff\nTEL:LDR:801:1500\n")
        self.assertEqual(self.arduino.ultima_leitura.bruto, 801)
        self.assertEqual(self.arduino.linhas_invalidas, 3)

    def test_cache_leitura_nao_acessa_porta_e_expira(self):
        self.arduino.serial = SerialSimulada()
        self.arduino._processar_bytes(PRONTO + b"TEL:LDR:900:500\n")
        self.assertEqual(self.arduino.ler_luminosidade(), 900)
        self.assertEqual(self.arduino.serial.leitores, set())
        self.assertEqual(self.arduino.serial.enviados, [])
        self.tempo = 4
        self.assertIsNone(self.arduino.ler_luminosidade())
        self.assertEqual(self.arduino.ultima_leitura_valida, 0)

    def test_telemetria_sem_capacidade_nao_e_valida(self):
        self.arduino._processar_bytes(b"PRONTO v2\nTEL:LDR:500:500\n")
        self.assertIsNone(self.arduino.ultima_leitura)

    def test_consulta_digital_legada_cache_sem_limpar_serial(self):
        self.arduino.serial = SerialSimulada()
        self.arduino._processar_bytes(b"PRONTO v3\nLUZ:1\n")
        self.assertEqual(self.arduino.ler_luminosidade(), 1)
        self.assertIn("legado", self.arduino.descrever_luminosidade())

    def test_thread_unica_leitor_reconexao_controlada_e_shutdown(self):
        placas = [SerialSimulada(), SerialSimulada()]
        fabrica = Mock(side_effect=placas)
        arduino = Arduino("COM_SIMULADA", fabrica_serial=fabrica, reconexao_s=1, sensor_tipo="analogico")
        self.addCleanup(arduino.fechar)
        self.assertTrue(arduino.sensor_analogico_disponivel)
        arduino.conectar()  # idempotente, sem outro leitor
        placas[0].fila.put(b"TEL:LDR:300:500\n")
        fim = time.monotonic() + 1
        while arduino.ultima_leitura is None and time.monotonic() < fim: time.sleep(.01)
        self.assertIsNotNone(arduino.ultima_leitura)
        placas[0].falha = True
        fim = time.monotonic() + .5
        while arduino.conectado and time.monotonic() < fim: time.sleep(.01)
        self.assertFalse(arduino.conectado)
        self.assertIsNone(arduino.ultima_leitura)
        self.assertEqual(fabrica.call_count, 1)
        fim = time.monotonic() + 2
        while fabrica.call_count < 2 and time.monotonic() < fim: time.sleep(.01)
        self.assertEqual(fabrica.call_count, 2)
        arduino.fechar()
        self.assertFalse(arduino._thread.is_alive())
        self.assertTrue(all(p.leitores <= {"arduino-serial"} for p in placas))

    def test_importar_script_sem_voz_camera_requests(self):
        codigo = """
import builtins
original = builtins.__import__
def importar(nome, *args, **kwargs):
    if nome.split('.')[0] in {'speech_recognition','cv2','requests','pyttsx3'}:
        raise ModuleNotFoundError(nome)
    return original(nome,*args,**kwargs)
builtins.__import__=importar
import testar_arduino
import maker_lab
"""
        r = subprocess.run([sys.executable, '-c', codigo], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
