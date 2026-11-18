"""Regressão: alertas por voz não dependem de reconhecimento ou microfone."""

import sys
import types
import unittest
from unittest.mock import Mock, patch

from modulos.voz import Voz
import maker_lab


class VozMakerTest(unittest.TestCase):
    def test_apenas_falar_funciona_sem_speech_recognition(self):
        with patch.dict(sys.modules, {"speech_recognition": None}), \
             patch.object(Voz, "_descobrir_voz_portugues", return_value=None):
            voz = Voz(escutar=False)
            self.addCleanup(voz.fechar)
            self.assertIsNone(voz.reconhecedor)
            with patch.object(voz, "_falar_edge", return_value=True) as falar:
                voz.falar("Teste de alerta.")
                falar.assert_called_once()

    def test_ouvir_sem_dependencia_da_orientacao_de_instalacao(self):
        with patch.dict(sys.modules, {"speech_recognition": None}), \
             patch.object(Voz, "_descobrir_voz_portugues", return_value=None):
            voz = Voz(escutar=False)
            self.addCleanup(voz.fechar)
            with self.assertRaisesRegex(RuntimeError, "SpeechRecognition PyAudio"):
                voz.ouvir()

    def test_reconhecedor_preserva_configuracao_da_assistente(self):
        reconhecedor = types.SimpleNamespace()
        biblioteca = types.SimpleNamespace(Recognizer=Mock(return_value=reconhecedor))
        with patch.dict(sys.modules, {"speech_recognition": biblioteca}), \
             patch.object(Voz, "_descobrir_voz_portugues", return_value=None):
            voz = Voz()
            self.addCleanup(voz.fechar)
            self.assertIs(voz.reconhecedor, reconhecedor)
            self.assertEqual(reconhecedor.pause_threshold, 1.8)
            self.assertEqual(reconhecedor.non_speaking_duration, 0.6)
            self.assertFalse(reconhecedor.dynamic_energy_threshold)
            voz._iniciar_reconhecimento()
            biblioteca.Recognizer.assert_called_once()

    def test_maker_ativa_apenas_saida_e_fecha_arduino(self):
        arduino = Mock()
        with patch.object(sys, "argv", ["maker_lab.py", "--voz", "--segundos", "1"]), \
             patch("modulos.arduino.Arduino", return_value=arduino), \
             patch("modulos.voz.Voz") as fabrica, \
             patch.object(maker_lab, "executar_monitoramento") as monitor:
            maker_lab.main()
        fabrica.verificar_sintese.assert_called_once()
        fabrica.assert_called_once_with(arduino=arduino, escutar=False)
        monitor.assert_called_once_with(arduino, voz=fabrica.return_value, segundos=1)
        arduino.fechar.assert_called_once()

    def test_falta_de_audio_da_instrucao_e_libera_porta(self):
        arduino = Mock()
        with patch.object(sys, "argv", ["maker_lab.py", "--voz"]), \
             patch("modulos.arduino.Arduino", return_value=arduino), \
             patch("modulos.voz.importlib.util.find_spec", return_value=None), \
             patch.object(maker_lab, "executar_monitoramento") as monitor:
            with self.assertRaisesRegex(RuntimeError, "requirements-maker.txt"):
                maker_lab.main()
        monitor.assert_not_called()
        arduino.fechar.assert_called_once()


if __name__ == "__main__":
    unittest.main()
