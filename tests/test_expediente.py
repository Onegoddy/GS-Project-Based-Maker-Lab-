"""Expediente: relógio, voz, armazenamento e IA locais, sem periféricos ou rede."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from modulos.config import BRASILIA, ConfigEnergia
from modulos.dialogo_energia import ControladorVozEnergia
from modulos.eventos import RegistroEnergia
from modulos.expediente import (Expediente, HorarioAmbiguo, HorarioInvalido,
                                interpretar_horario, extrair_horarios_frase)
from modulos.monitoramento_energia import MonitorEnergia
from test_energia import DECISAO


class VozDuble:
    def __init__(self, respostas=()):
        self.respostas, self.frases, self.capturas = iter(respostas), [], []
        self.em_dialogo = False

    def falar(self, texto):
        self.frases.append(texto)

    def ouvir(self, **kwargs):
        self.capturas.append(kwargs)
        resposta = next(self.respostas, "")
        if isinstance(resposta, Exception):
            raise resposta
        return resposta

    @contextmanager
    def dialogo(self):
        self.em_dialogo = True
        try:
            yield
        finally:
            self.em_dialogo = False


class HorariosTest(unittest.TestCase):
    def test_portugues_e_notacao_vinte_quatro_horas(self):
        exemplos = {"sete da manhã": "07:00", "às nove da noite": "21:00",
            "sete e meia da manhã": "07:30", "duas e um quarto da tarde": "14:15",
            "vinte e uma e trinta": "21:30", "vinte e três": "23:00",
            "07:00": "07:00", "21:00": "21:00", "21h30": "21:30",
            "meia-noite": "00:00", "meio dia": "12:00", "uma da madrugada": "01:00",
            "doze da noite": "00:00", "dezoito horas": "18:00"}
        for texto, esperado in exemplos.items():
            with self.subTest(texto=texto):
                self.assertEqual(interpretar_horario(texto), esperado)

    def test_horas_ambiguas_exigem_periodo(self):
        for texto in ("às sete", "7", "sete horas", "sete e meia", "doze", "7h"):
            with self.subTest(texto=texto), self.assertRaises(HorarioAmbiguo):
                interpretar_horario(texto)

    def test_horarios_invalidos_e_iguais(self):
        for texto in ("24:00", "21:99", "21:", "sete ou oito da manhã", "vinte e cinco",
                      "nove da madrugada", "vinte e uma da manhã", "07:00 da noite", "banana", ""):
            with self.subTest(texto=texto), self.assertRaises(HorarioInvalido):
                interpretar_horario(texto)
        with self.assertRaises(HorarioInvalido):
            Expediente("07:00", "07:00")

    def test_frase_direta_e_expediente_noturno(self):
        a, f = extrair_horarios_frase("Madara, a loja abre às sete da manhã e fecha às nove da noite.")
        self.assertEqual((interpretar_horario(a), interpretar_horario(f)), ("07:00", "21:00"))
        self.assertTrue(Expediente("22:00", "06:00").atravessa_meia_noite)
        cfg = ConfigEnergia(abertura="22:00", fechamento="06:00")
        for hora, aberta in ((21, False), (22, True), (0, True), (5, True), (6, False)):
            self.assertEqual(cfg.loja_aberta(datetime(2026, 11, 16, hora, tzinfo=BRASILIA)), aberta)


class ExpedienteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tempo = 0.
        self.base = datetime(2026, 11, 16, 22, tzinfo=BRASILIA)
        self.ia = Mock()
        self.ia.analisar_iluminacao.return_value = DECISAO
        self.cfg = ConfigEnergia(sensor_tipo="digital", referencia_escuro=1, referencia_claro=0,
            abertura="07:00", fechamento="21:00", persistencia_s=2, intervalo_ia_s=.1,
            potencia_kw=.08, tarifa_rs_kwh=.95)
        self.monitor = self.novo()

    def novo(self, config=None):
        return MonitorEnergia(self.ia, config or self.cfg, RegistroEnergia(self.tmp.name),
            relogio=lambda: self.tempo, agora=lambda: self.base + timedelta(seconds=self.tempo))

    def atualizar(self, bruto, tempo):
        self.tempo = tempo
        return self.monitor.atualizar(bruto)

    def controlador(self, respostas):
        voz = VozDuble(respostas)
        return ControladorVozEnergia(self.monitor, voz, self.ia, relogio=lambda: self.tempo,
                                   espera_s=8, total_s=120), voz

    def ativar_ocorrencia(self, analisar=True):
        for t in range(4):
            self.atualizar(0, t)
        pedido = self.monitor.pedidos.get_nowait()
        if analisar:
            self.monitor.analisar_pedido(pedido)
            self.atualizar(0, 4)
        return self.monitor._ativa, pedido

    def test_fluxo_guiado_separado_confirma_e_persiste_sem_env(self):
        env = Path(self.tmp.name) / ".env"
        env.write_text("SEGREDO=preservado\nMAKER_ABERTURA=07:00\n", encoding="utf-8")
        c, voz = self.controlador(["sete da manhã", "nove da noite", "confirmo"])
        self.assertFalse(c.processar_fala("configurar horário de funcionamento"))
        c.processar_fala("Madara, configurar horário de funcionamento")
        self.assertTrue(any("horário de abertura" in f for f in voz.frases))
        self.assertTrue(any("horário de fechamento" in f for f in voz.frases))
        self.assertTrue(any("Entendi: abertura às 07:00 e fechamento às 21:00" in f for f in voz.frases))
        self.assertIn("sem Madara", voz.frases[0])
        self.assertFalse(voz.em_dialogo)
        self.assertTrue(all(c["timeout"] <= 8 for c in voz.capturas))
        salvo = json.loads((Path(self.tmp.name) / "expediente.json").read_text(encoding="utf-8"))
        self.assertEqual(salvo["abertura"], "07:00")
        self.assertNotIn("SEGREDO", str(salvo))
        self.assertIn("SEGREDO=preservado", env.read_text(encoding="utf-8"))

    def test_frase_direta_so_pede_confirmacao_e_aceita_nome(self):
        c, voz = self.controlador(["Madara, confirmo"])
        c.processar_fala("Madara, a loja abre às dez da noite e fecha às seis da manhã")
        self.assertEqual(len(voz.capturas), 1)
        e = self.monitor.consultar_expediente()
        self.assertEqual((e["abertura"], e["fechamento"]), ("22:00", "06:00"))
        self.assertTrue(any("dia seguinte" in f for f in voz.frases))

    def test_ambiguidade_esclarecida_antes_de_confirmar(self):
        c, voz = self.controlador(["sete", "sete da manhã", "21:00", "confirmo"])
        c.configurar_expediente("configurar horário de funcionamento")
        self.assertEqual(self.monitor.consultar_expediente()["fonte"], "expediente.json")
        self.assertTrue(any("Informe também" in f for f in voz.frases))

    def test_cancelamento_falhas_ambiguidades_e_sem_confirmacao_nao_alteram(self):
        casos = (["cancelar"], ["07:00", "cancelar"], ["07:00", "21:00", "não"],
            ["07:00", "21:00", "talvez", "talvez"], ["07:00", "21:00", ""],
            ["sete", "sete"], ["07:00", RuntimeError("captura falhou")],
            ["07:00", "07:00"], [""])
        for respostas in casos:
            with self.subTest(respostas=respostas):
                c, voz = self.controlador(respostas)
                c.configurar_expediente("configurar horário de funcionamento")
                self.assertEqual(self.monitor.consultar_expediente()["fonte"], "configuracao_inicial")
                self.assertFalse((Path(self.tmp.name) / "expediente.json").exists())
                self.assertFalse(voz.em_dialogo)

    def test_expiracao_da_janela_nao_aplica_confirmacao_tardia(self):
        c, voz = self.controlador(["07:00", "21:00", "confirmo"])
        ouvir = voz.ouvir
        def atrasar(**kwargs):
            texto = ouvir(**kwargs)
            if texto == "confirmo":
                self.tempo += 121
            return texto
        voz.ouvir = atrasar
        c.configurar_expediente("configurar horário de funcionamento")
        self.assertEqual(self.monitor.consultar_expediente()["revisao"], 0)

    def test_cancelamento_preserva_expediente_previamente_salvo(self):
        self.monitor.atualizar_expediente("22:00", "06:00", confirmado=True)
        salvo = (Path(self.tmp.name) / "expediente.json").read_bytes()
        c, _ = self.controlador(["08:00", "19:00", "cancelar"])
        c.configurar_expediente("configurar horário de funcionamento")
        self.assertEqual((Path(self.tmp.name) / "expediente.json").read_bytes(), salvo)
        self.assertEqual(self.monitor.config.abertura, "22:00")

    def test_salvo_prevalece_sobre_config_inicial_e_preserva_sensor(self):
        self.monitor.atualizar_expediente("22:00", "06:00", confirmado=True)
        novo = self.novo(replace(self.cfg, abertura="08:00", fechamento="18:00"))
        self.assertEqual(novo.config.abertura, "22:00")
        self.assertEqual(novo.config.fechamento, "06:00")
        self.assertEqual(novo.config.sensor_tipo, "digital")
        self.assertEqual(novo.config.referencia_escuro, 1)
        self.assertTrue(novo.config.loja_aberta(self.base))

    def test_arquivo_salvo_invalido_e_outra_loja_nao_sao_ignorados(self):
        self.monitor.atualizar_expediente("22:00", "06:00", confirmado=True)
        with self.assertRaisesRegex(ValueError, "outra loja"):
            self.novo(replace(self.cfg, loja_id="padaria-01", equipamento_id="iluminacao-salao"))
        (Path(self.tmp.name) / "expediente.json").write_text("{invalido", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "JSON inválido"):
            self.novo()

    def test_sem_confirmacao_ou_revisao_obsoleta_nao_persiste(self):
        with self.assertRaises(ValueError):
            self.monitor.atualizar_expediente("22:00", "06:00")
        self.monitor.atualizar_expediente("22:00", "06:00", confirmado=True)
        with self.assertRaisesRegex(ValueError, "mudou durante"):
            self.monitor.atualizar_expediente("08:00", "19:00", confirmado=True, revisao_esperada=0)
        self.assertEqual(self.monitor.config.abertura, "22:00")

    def test_falha_substituicao_preserva_arquivo_runtime_e_ocorrencia(self):
        self.monitor.atualizar_expediente("08:00", "20:00", confirmado=True)
        eid, _ = self.ativar_ocorrencia()
        caminho = Path(self.tmp.name) / "expediente.json"
        anterior, config = caminho.read_bytes(), self.monitor.config
        with patch.object(Path, "replace", side_effect=PermissionError("arquivo bloqueado")):
            with self.assertRaises(PermissionError):
                self.monitor.atualizar_expediente("22:00", "06:00", confirmado=True)
        self.assertIs(self.monitor.config, config)
        self.assertEqual(self.monitor._ativa, eid)
        self.assertEqual(caminho.read_bytes(), anterior)
        self.assertFalse(list(Path(self.tmp.name).glob(".expediente-*.tmp")))

    def test_mudanca_encerra_ocorrencia_audita_e_nao_avisa_atrasado(self):
        eid, _ = self.ativar_ocorrencia()
        resultado = self.monitor.atualizar_expediente("22:00", "06:00", confirmado=True)
        self.assertEqual(resultado["ocorrencia_encerrada"], eid)
        self.assertFalse(self.monitor.aviso_atual(eid))
        self.assertEqual(self.atualizar(0, 5).estado, "dentro_do_horario")
        eventos = self.monitor.registro.ler("eventos.jsonl")
        self.assertEqual([e["id"] for e in eventos], [eid])
        self.assertEqual(eventos[0]["duracao_s"], 4)
        auditoria = [a for a in self.monitor.registro.ler("auditoria.jsonl") if a["estado"] == "expediente_alterado"]
        self.assertEqual(auditoria[-1]["ocorrencia_encerrada"], eid)
        self.assertEqual(self.monitor.contexto_energia()["ultima_ocorrencia"]["motivo_encerramento"], "expediente_alterado")

    def test_analise_em_andamento_encerra_sem_perder_evento_ou_falar(self):
        eid, pedido = self.ativar_ocorrencia(analisar=False)
        self.monitor.atualizar_expediente("22:00", "06:00", confirmado=True)
        self.monitor.analisar_pedido(pedido)
        self.atualizar(0, 5)
        self.assertEqual([e["id"] for e in self.monitor.registro.ler("eventos.jsonl")], [eid])
        self.assertTrue(self.monitor.avisos.empty())

    def test_novo_expediente_fora_do_horario_reinicia_persistencia(self):
        eid, _ = self.ativar_ocorrencia()
        self.monitor.atualizar_expediente("08:00", "20:00", confirmado=True)
        self.assertEqual(self.atualizar(0, 5).estado, "observando")
        self.assertIsNone(self.monitor._ativa)
        self.atualizar(0, 7)
        self.assertNotEqual(self.monitor._ativa, eid)

    def test_mudar_expediente_preserva_pausa_e_coleta(self):
        self.monitor.pausar()
        self.monitor.atualizar_expediente("22:00", "06:00", confirmado=True)
        self.assertTrue(self.monitor.pausado)
        self.assertEqual(self.atualizar(0, 1).estado, "pausado")
        self.assertTrue(self.monitor.registro.ler("leituras.jsonl"))
        self.monitor.retomar()
        self.assertEqual(self.atualizar(0, 2).estado, "dentro_do_horario")

    def test_confirmar_mesmos_horarios_preserva_ocorrencia(self):
        eid, _ = self.ativar_ocorrencia()
        resultado = self.monitor.atualizar_expediente("07:00", "21:00", confirmado=True)
        self.assertIsNone(resultado["ocorrencia_encerrada"])
        self.assertEqual(self.monitor._ativa, eid)
        self.assertTrue(any(a["estado"] == "expediente_confirmado"
                            for a in self.monitor.registro.ler("auditoria.jsonl")))

    def test_auditoria_falha_depois_do_commit_retentada_sem_mentir(self):
        gravar = self.monitor.registro.gravar
        def falhar(nome, dados):
            if dados.get("estado") == "expediente_alterado":
                raise PermissionError("teste")
            return gravar(nome, dados)
        with patch.object(self.monitor.registro, "gravar", side_effect=falhar):
            resultado = self.monitor.atualizar_expediente("22:00", "06:00", confirmado=True)
        self.assertTrue(resultado["auditoria_pendente"])
        self.assertEqual(self.monitor.config.abertura, "22:00")
        self.assertEqual(self.monitor.estado, "falha_registro")
        self.monitor.regravar_resultados()
        self.assertFalse(self.monitor._auditorias_pendentes)
        self.assertEqual(len([a for a in self.monitor.registro.ler("auditoria.jsonl")
                              if a["estado"] == "expediente_alterado"]), 1)
