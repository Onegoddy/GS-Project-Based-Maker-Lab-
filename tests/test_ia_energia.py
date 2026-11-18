"""Provedores com transporte HTTP dublê; nenhuma rede ou chave real."""
import io
import json
import unittest
from unittest.mock import patch
from modulos.ia_generativa import IAGenerativa
from modulos.ia_iluminacao import IAIluminacao, INSTRUCAO
from test_energia import DECISAO

class IATest(unittest.TestCase):
    def test_normaliza_espacos_unicode_sem_alterar_conteudo(self):
        dados = {**DECISAO, "mensagem_falada": "Confira\u202fa\u00a0iluminação."}
        self.assertEqual(IAIluminacao.validar(dados)["mensagem_falada"], "Confira a iluminação.")

    def test_contrato_rejeita_campos_ausentes_tipo_tamanho_e_extras(self):
        for d in ([], {}, {**DECISAO, "mensagem_falada": ""},
                  {**DECISAO, "classificacao": "inconclusivo"},
                  {**DECISAO, "extra": 1}, {**DECISAO, "mensagem_falada": "x"*281},
                  {**DECISAO, "mensagem_falada": "O estoque está vazio."},
                  {**DECISAO, "mensagem_falada": "Desligue a luz."},
                  {**DECISAO, "justificativa": "texto\ncomando"}):
            with self.subTest(d=d), self.assertRaises(ValueError): IAIluminacao.validar(d)

    def test_nao_afirma_iluminacao_acesa_nem_complementos_de_local(self):
        for texto in ("A iluminação no estoque permanece acesa fora do expediente.",
                      "A luz do estoque continua ligada.", "As lâmpadas da loja estão acesas."):
            with self.subTest(texto=texto), self.assertRaises(ValueError):
                IAIluminacao.validar({**DECISAO, "mensagem_falada": texto})
        segura = {**DECISAO, "mensagem_falada": "Há claridade persistente fora do expediente. Verifique se há lâmpadas acesas e se são necessárias."}
        self.assertEqual(IAIluminacao.validar(segura), segura)

    def test_groq_contexto_prompt_separado_timeout_e_json(self):
        resposta = io.BytesIO(json.dumps({"choices":[{"message":{"content":json.dumps(DECISAO)}}]}).encode())
        contexto = {"leitura_adc":900,"qualidade":"valida_e_calibrada"}
        with patch('modulos.ia_iluminacao.obter_variavel', side_effect=lambda n,p=None: 'chave-teste' if n=='GROQ_API_KEY' else p), \
             patch('modulos.ia_iluminacao.urlopen', return_value=resposta) as http:
            r = IAGenerativa().analisar_iluminacao(contexto, timeout_s=2)
        self.assertEqual(r, DECISAO)
        corpo = json.loads(http.call_args.args[0].data)
        self.assertEqual(json.loads(corpo['messages'][1]['content']), contexto)
        self.assertEqual(corpo['messages'][0]['content'], INSTRUCAO)
        self.assertNotIn('ninja', INSTRUCAO)
        self.assertEqual(corpo['response_format']['type'], 'json_schema')
        self.assertTrue(corpo['response_format']['json_schema']['strict'])
        self.assertFalse(corpo['response_format']['json_schema']['schema']['additionalProperties'])
        self.assertEqual(corpo['reasoning_effort'], 'low')
        self.assertEqual(http.call_args.kwargs['timeout'], 2)

    def test_ollama_usa_mesma_operacao_json_e_timeout(self):
        resposta = io.BytesIO(json.dumps({'response':json.dumps(DECISAO)}).encode())
        with patch('modulos.ia_iluminacao.urlopen', return_value=resposta) as http:
            r = IAGenerativa(provedor='ollama').analisar_iluminacao({'leitura_adc':700}, timeout_s=3)
        self.assertEqual(r, DECISAO)
        corpo=json.loads(http.call_args.args[0].data)
        self.assertEqual(corpo['format'], 'json')
        self.assertFalse(corpo['stream'])
        self.assertEqual(json.loads(corpo['prompt'])['leitura_adc'],700)
        self.assertEqual(http.call_args.kwargs['timeout'],3)

    def test_sem_chave_falha_sem_chamar_rede(self):
        with patch('modulos.ia_iluminacao.obter_variavel', side_effect=lambda n,p=None: p), \
             patch('modulos.ia_iluminacao.urlopen') as http:
            with self.assertRaises(ValueError): IAGenerativa().analisar_iluminacao({})
        http.assert_not_called()
