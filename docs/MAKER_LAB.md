# Integração Maker Lab — modos digital e analógico contínuos

## Fontes e prioridade

Foram lidos os PDFs locais completos por extração de texto, com inspeção visual
 das páginas relevantes: enunciado oficial (60 páginas) e guia (14 páginas).
A tabela distingue exigência acadêmica, plano da equipe e decisão desta implementação.

| Origem | Conteúdo | Evidência/decisão neste repositório |
|---|---|---|
| Oficial, p. 20 | Ficha Técnica no início do README/documentação | Ficha no README; este documento detalha seu componente físico |
| Oficial, p. 54–55 | Evoluir robô, sensor adicional, IA interpretando dados e resposta contextualizada | Uno/corpo existentes, LDR digital ou analógico, operação específica de `IAGenerativa` |
| Oficial, p. 55–57 | TinyML, voz, LLMs e exemplos de sensores são possibilidades | Escolhida LLM no PC/nuvem; não há obrigação de TinyML no Uno |
| Oficial, p. 58 | GitHub com instalação, componentes e integração; vídeo de até 6 min; nomes no README | Código, instruções e integrantes no repositório; vídeo físico vinculado no README |
| Oficial, p. 59 | Sensor, IA, integração, CitizenTech, autonomia, acabamento, testes/falhas e criatividade | Testes de software e demonstração de bancada; o vídeo mostra sensor, interação, alerta, LEDs e cabeça |
| Guia, seções 1 e 11 | Iluminação de comércio, LDR com horário ou PIR, aviso e registro | Escolhido horário, estoque com pouca luz natural; PIR fora do MVP |
| Guia, seção 3 | IDs, evento JSONL e fórmula de custo | Mesmo contrato exportável; custo sem parâmetros fica pendente localmente |
| Implementação/solicitação | DO→8 ou A0 conforme montagem, telemetria, filas, histerese, tempos, contingência | Valores e políticas configuráveis documentados; não são exigências textuais da disciplina |

O exemplo de aviso do guia menciona estoque vazio. Ele não é repetido aqui:
a ausência de pessoas não é medida. O guia sugere desconectar sensor como falha;
uma entrada analógica solta pode continuar dentro de 0..1023. Os testes cobrem perda
de comunicação e indisponibilidade de IA, sem prometer diagnóstico elétrico do LDR.
O enunciado e a orientação do professor prevalecem sobre o guia.

## Montagem confirmada pela equipe

O usuário informou **VCC→5V, GND→GND, DO→8**. O `.env` local foi ajustado
apenas com `MAKER_SENSOR_TIPO=digital`, preservando as outras entradas e sem
inventar referências. Os valores ADC próximos de 235 relatados anteriormente
eram leituras de A0; esse não era o pino conectado ao sensor informado.

O modo digital consulta o bit real DO a cada 1 s e calibra qual bit significa
escuro/iluminado. Um mesmo nível nas duas condições é rejeitado. Não se
transforma DO em ADC 0/1023, lux ou intensidade contínua. Se houver trimpot,
o limiar óptico é ajustado no próprio módulo. O filtro temporal continua no PC.
O modo analógico continua disponível por seleção explícita `analogico`, com
AO/divisor em A0 e referências ADC medidas. Falhas não trocam o modo automaticamente.

## Módulos e tarefas

| Arquivo | Responsabilidade |
|---|---|
| `arduino/madara_arduino/madara_arduino.ino` | ADC por `millis()`, consulta DO, serial, fade dos LEDs e motor existentes |
| `modulos/arduino.py` | Um leitor da serial; framing, capacidades, cache datado, reconexão e comandos |
| `modulos/config.py` | `.env`/ambiente, validação da calibração, expediente e parâmetros |
| `modulos/monitoramento_energia.py` | Máquina temporal e serviço com tarefas independentes de avaliação, IA e avisos |
| `modulos/ia_generativa.py` | Operação `analisar_iluminacao`, independente do prompt antigo de personagem |
| `modulos/ia_iluminacao.py` | Transporte HTTP, instrução de energia e validação de quatro campos |
| `modulos/eventos.py` | JSONL, custo, deduplicação, checkpoint e exportação de pendências |
| `modulos/voz.py` | Fila única; exclusão entre captura/reprodução; temporário exclusivo e limpeza |
| `modulos/intencoes.py` | Energia antes de horas/tempo/calcular; encerramento explícito |
| `modulos/expediente.py` | Horários em português, ambiguidade e arquivo próprio de expediente |
| `modulos/dialogo_energia.py` | Comandos locais, confirmação por voz e contexto mínimo para conversa |
| `modulos/consultor_energia.py` | Prompt profissional independente, escopo e contrato de conversa |
| `maker_lab.py` | Entrada física sem dependências de microfone/câmera e simulação explícita |
| `madara.py` | Interface antiga e monitor simultâneo, pausa/retomada e consultas locais |

O leitor serial apenas interpreta/protege o cache. Não faz HTTP, áudio ou escrita
 de arquivos. O avaliador do PC consulta o cache (0,2 s por padrão); registra
amostras novas que observa e mantém as durações mesmo quando o microfone está
em silêncio ou a assistente está ocupada. Em sobrecarga do PC, snapshots podem
pular amostras; isto não é um instrumento para medir formas de onda elétricas.

Uma fila limitada leva contextos relevantes à tarefa de IA. Uma ocorrência faz
uma tarefa de análise, com espaçamento configurado entre ocorrências. Pode
haver uma revisão de conteúdo dentro dessa tarefa e do mesmo orçamento de tempo. Pendências não
são descartadas quando a fila está cheia; permanecem no checkpoint e voltam a
ser consideradas nas próximas avaliações. Se uma condição terminar antes da
consulta, seu contexto é identificado como histórico. A idade dos dados é
recalculada antes de consultar a LLM. Uma resposta tardia nunca inicia fala de
uma ocorrência já encerrada.

Os avisos seguem outra fila; com voz, entram na fila única de `Voz`, compartilhada
com todas as funções antigas. A condição é reavaliada antes de começar cada aviso
falado. A escuta tem espera inicial limitada a 1 s, limite de frase e timeout de
operação de reconhecimento de 5 s. Captura e reprodução usam a mesma trava,
com pausa acústica posterior. Isso evita capturar a própria reprodução; a
qualidade acústica ainda deve ser ensaiada no boneco.

A espera de 1 s é um limite lógico de uma tentativa, sem fechar/reabrir o
dispositivo a cada silêncio. `Voz` mantém uma sessão de `Microphone` sob a
mesma exclusão de áudio e imprime “Ouvindo…” só ao entrar na sessão. Calibra
ruído uma vez, preservando os parâmetros existentes. Depois de uma frase,
fecha a sessão antes do reconhecimento/roteamento, sem guardar áudio de fundo
enquanto a assistente está ocupada. A reprodução fecha uma sessão ociosa antes
de falar, e a retomada conserva a pausa acústica. Um aviso cancelado antes da
reprodução não fecha a sessão silenciosa sem necessidade.

Falha de captura libera o dispositivo e permite recalibração na próxima
abertura. Uma abertura sem stream válido causa erro explícito. Ctrl+C e
`Voz.fechar()` liberam a sessão; chamadas após fechamento não a reabrem.
O transporte Google de reconhecimento não mudou. Não há captura e reprodução
simultâneas, arquivo de áudio novo ou nova tarefa de microfone em segundo plano.

O aviso automático também passa um efeito de cabeça pela mesma fila de áudio.
`Arduino.cabeca_balancando` mantém os argumentos antigos e recebe limites
opcionais de duração/condição. Por padrão, o aviso usa 30 ms por trecho e até
2 s, com checagem de cancelamento a cada no máximo 50 ms entre comandos.
O prazo usa monotônico; a tarefa do gesto envia `P` em `finally`, inclusive
quando o prazo termina antes da fala. Um contexto de efeito é consumido uma
vez por aviso, para não repetir o gesto após falha do sintetizador online.
Sem voz, o mesmo efeito acompanha os LEDs. Leitura serial, avaliação e escrita
de ocorrências continuam independentes. Não há nova thread além da tarefa de
motor já usada pelo balanço legado, nem alteração do protocolo/firmware.

`ConfigEnergia` valida `motor_aviso` (booleano), `motor_balanco_ms` (10..60 ms)
e `motor_duracao_s` (>0 e até 5 s). Os padrões ativam a função sem editar `.env`;
`MAKER_MOTOR_AVISO=false` a desativa. Calibração DO→8 e
`ARDUINO_BALANCO_MS` das cenas antigas ficam independentes. O retorno ao centro
é estimado pelo firmware, não medido; teste a interferência óptica do movimento.

## Protocolo serial e comunicação

- Uno, USB, 9600 baud, linhas ASCII terminadas em LF (CRLF aceito).
- Inicialização/`H`: `PRONTO v4 CAPS=LEDS_SEPARADOS,LDR_ANALOGICO,LUZ_DIGITAL`.
- A cada 500 ms: `TEL:LDR:<ADC>:<millis>`, ADC 0..1023, uptime unsigned 32 bits.
- `O/o`: ambos LEDs ligar/apagar; `V/A`: vermelho/azul; `G/P`: cabeça girar/parar.
- `L`: consulta `LUZ:0/1` no 8; o leitor único solicita a cada 1 s e mantém cache digital datado, separado do ADC.
- Python aceita efeitos de PRONTO antigo e v2/v3; LEDs permanecem disponíveis
  para versões posteriores. Telemetria analógica exige capacidade anunciada.

Somente `arduino-serial` usa `read()`. Consultas antigas agora usam cache; não
há `reset_input_buffer()` descartando telemetria. Mensagens incompletas são
acumuladas até LF; linha maior que 192 bytes é descartada inteira até o próximo
LF. Mensagens inválidas/inesperadas têm contadores e não renovam atualidade.

Abertura/reset espera handshake até cerca de 3 s; `H` permite renegociar após
boot. Falhas de I/O invalidam cache/capacidades e levam à reconexão, padrão 5 s.
Uma placa com sensor configurado silencioso é reaberta após 10 s sem leitura válida
da entrada selecionada; tráfego ADC não renova o cache DO nem esconde sua ausência;
o monitor já considera dados indisponíveis após 3 s (parâmetros independentes).
A ausência inicial da placa não impede a assistente de iniciar. Não há substituição
silenciosa por dados sintéticos. Mensagens de conexão são por mudança de estado.

## Máquina temporal e limitações

1. Sem amostra recente/suporte ao tipo configurado: `indisponivel`, nunca normal.
2. Sem referências medidas: `nao_calibrado`, sem IA ou evento de desperdício.
3. Analógico: escala relativa calibrada e histerese na faixa intermediária.
   Digital: estado binário pela polaridade medida; limiar óptico fica no módulo.
4. Claro durante expediente não configura suspeita automaticamente.
5. Claro fora do expediente inicia candidato; oscilação curta não abre ocorrência.
6. Persistência confirmada abre UUID estável e solicita interpretação contextual.
7. `possivel_desperdicio` pode avisar uma vez; `normal`/`dados_insuficientes`
   ficam na auditoria quando compatíveis com o contexto, como observação histórica.
   Uma falha técnica ou classificação incompatível ativa contingência identificada.
8. Escuro, abertura da loja, pausa, perda de dados ou encerramento fecham o
   período observado. Falta de dados não é tratada como resolução física.
9. Retomada/recuperação da comunicação iniciam nova observação; não se conta
   o intervalo sem dados nem o tempo em que o programa esteve desligado.

O aviso pede verificação humana e não atua na instalação. Horário não comprova
ocupação; luz pode ser necessária para segurança, limpeza ou jornada extra.
Não há lux, corrente ou consumo medido; não há medição de economia.
A0 desconectado pode parecer válido; extremo/valor estável não prova falha.

## IA e resiliência

A operação da IA recebe o tipo do sensor, valores reais calibrados, loja/ambiente, expediente,
duração e qualidade/idade. As regras temporais e os cálculos ficam no Python.
No modo digital, `leitura_adc` e `luminosidade_relativa` são nulos;
`leitura_digital` mantém o bit real e `ambiente_claro` indica sua interpretação calibrada.
O prompt é profissional e separado das instruções Naruto do chatbot antigo.
Para GPT-OSS 20B/120B na Groq, usa JSON Schema estrito; a validação local dos
campos e conteúdo permanece obrigatória. O esforço de raciocínio é baixo e a
geracão tem limite de 1024 tokens. Outros modelos Groq mantêm JSON Object Mode.
A resposta exige exatamente `classificacao`, `justificativa`, `recomendacao`,
`mensagem_falada`, com tipos/tamanhos limitados e categorias fechadas.
Há barreiras textuais conservadoras para afirmações explícitas de ocupação,
lâmpada ligada, consumo medido e ordens diretas de desligamento; isso não é uma
prova de correção semântica da LLM. Avaliar as respostas reais faz parte da
validação do protótipo.

Groq/Ollama são acessados por HTTP com timeout de socket configurável (até 30 s)
e resposta/tokens limitados. Erros de rede, cota e autenticação não têm retries
automáticos. Há no máximo uma revisão para falhas de formato/conteúdo, com
feedback do campo/regra e o restante do orçamento da operação. Sem tempo ou
com segunda resposta inválida, a análise falha e segue a contingência.
A coleta não espera a chamada. Esse orçamento não transforma o timeout de
socket em garantia de prazo absoluto contra um servidor que envie dados lentamente.
Timeout, chave ausente, JSON inválido e outros erros de análise são auditados
por tipo/categoria e código HTTP, sem registrar corpos HTTP, tokens ou credenciais.
`json_validate_failed` é distinguido de autenticação, limite de uso e indisponibilidade.
`--diagnostico-ia` consulta só a lista de modelos, sem inferência ou Arduino. A contingência usa os
fatos observados e é marcada `contingencia_deterministica`, inclusive no aviso.
Não se afirma que houve análise por IA quando ela falhou.

O validador distingue afirmações positivas de ressalvas/verificação humana,
como “não se pode afirmar que a iluminação está acesa”. Uma ressalva não
libera alegações ou ordens posteriores na mesma resposta. Erros públicos têm
categoria/campo, sem corpo HTTP. A auditoria `execucao_ia` registra tentativas,
motivos seguros da revisão e aprovação da saída; o monitor ainda confere
compatibilidade contextual antes de considerar a análise válida.

O intervalo entre análises é separado do intervalo entre avisos. Uma nova
ocorrência pode ser analisada enquanto o cooldown global do aviso ainda não
terminou. Esse adiamento agora imprime uma mensagem e registra
`aviso_adiado_intervalo`, uma vez por ocorrência, com segundos restantes e
previsão civil para diagnóstico. A liberação continua controlada por tempo
monotônico; a previsão civil não comanda a duração. O resumo/diagnóstico
expõe `aguardando_intervalo`; `solicitado` representa enfileiramento, não
prova de som. Resolução ou encerramento antes da liberação cancela o aviso.

Na ocorrência ativa, `condicao_calibrada`, `interpretacao_sensor`,
`persistencia_confirmada` e `criterio_atencao` tornam explícito o que o
avaliador determinou. Um DO zero pode significar iluminado; não se exige ADC
para esse modo. Claridade persistente calibrada fora do expediente é um
indício, mesmo sem medir consumo/presença. A validação contextual rejeita
`normal`/`dados_insuficientes` para esse critério ativo e válido, preserva a
decisão rejeitada na auditoria e emite contingência rotulada. Não altera o
texto da IA para fingir uma decisão válida. O guard não se aplica a contexto
histórico, desatualizado, no expediente ou sem persistência confirmada.

No encerramento, o serviço interrompe avaliação/avisos, aguarda a chamada já
em curso pelo prazo e finaliza checkpoints. Se uma tarefa não encerrar, o código
relata erro e conserva checkpoint. Saída de áudio é interrompida, fila cancelada,
olhos apagados e serial fechada. Falha de disco é visível como `falha_registro`;
não é convertida em estado normal ou sucesso de gravação.

## Contrato e estimativa

O evento exportável é final, com oito campos do guia e custo numérico. Leituras,
contextos, falhas, origem da análise e razões de encerramento ficam separados.
`duracao_s` é inteira positiva: período observado desde o candidato até o fim
ou última amostra válida, nunca tempo anterior ao monitor. Uma ocorrência ativa
vive em `estado_ativo.json`, atualizado atomicamente. Reinício fecha o último
checkpoint como interrupção, mantém UUID e deduplica a exportação por ID.
Pedidos sem análise concluída ficam auditados, sem decisão inventada.

Fórmula de iluminação: `potencia_kw × duracao_s / 3600 × tarifa_rs_kwh`.
Não se reutiliza o fator de perda de refrigeração. Os parâmetros são cadastrados,
não medidos pelo robô. Ausentes, os eventos ficam em `eventos_pendentes.jsonl`
com `custo_estimado_rs: null`, extensão exclusivamente local. O contrato comum
continua em `eventos.jsonl`, com número; `--exportar-pendentes` calcula depois de
cadastrar os parâmetros e mantém os IDs. Resultados de IA cuja gravação falhou ficam pendentes em memória e são
regravados sem repetir a chamada da LLM. Checkpoints usam temporários exclusivos
e quatro tentativas curtas de substituição se houver PermissionError; o anterior
permanece válido se todas falharem. Contexto a cada tick não força nova substituição
quando a duração em segundos e o estado são os mesmos.

O arquivo de pendências é histórico,
pode continuar contendo itens já exportados; novas exportações ignoram esses IDs.

A pasta física é `dados/maker`, e simulação v4 usa `dados/maker/simulacao_v4`.
A pasta pode ser selecionada explicitamente por `--pasta` ou `MAKER_DADOS_DIR`,
sem mover dados antigos; isso permite testar fora do OneDrive. Bloqueio de
arquivos também pode ter outras causas, não diagnosticáveis só por PermissionError.
Um processo escritor por pasta. JSONL não possui coordenação entre processos;
não execute duas instâncias sobre os mesmos arquivos. Dados de execução não
são versionados. A coleta cresce continuamente; arquive a pasta com o monitor
parado conforme a política de retenção escolhida pela equipe.

## Privacidade e validação pendente

O modo Maker padrão não usa câmera ou microfone. Groq recebe contexto operacional
configurado; Ollama processa localmente. Logs não recebem chave nem áudio.
Reconhecimento facial antigo não participa desta análise.

## Extensão por microfone (16/11/2026)

`--voz --microfone` habilita explicitamente comandos no Maker sem câmera;
`--voz` continua apenas saída. A entrada completa reutiliza o controlador de
energia e conserva suas demais funções. O reconhecimento já existente envia
áudio ao Google; a opção de síntese offline não torna o reconhecimento offline.

O expediente segue Brasília e repetição diária. `expediente.json` confirmado
prevalece apenas sobre os horários iniciais do ambiente/`.env`; sem arquivo,
valem os iniciais. Schema versão 1, loja/equipamento, HH:MM distintos, fuso e
instante de alteração. JSON inválido/identidade divergente causa erro explícito.
O diálogo anuncia janela sem wake word (8 s para início, frase até 15 s,
total 120 s); fora dele exige Madara. Captura vazia/erro, confirmação ambígua,
cancelamento e expiração não aplicam a alteração.

O monitor salva primeiro em temporário com fsync/replace; só então troca o
objeto de configuração sob a trava do avaliador. Confirmação usa a revisão
capturada no começo do diálogo para recusar uma atualização concorrente.
Falha de persistência deixa o estado anterior intacto. Mudança real encerra
o episódio ativo em sua última leitura válida, conserva UUID e parâmetros
de custo e audita horários anterior/novo e ocorrência encerrada. Uma análise
em andamento permanece registrável, sem fala tardia. Candidato e histerese
reiniciam; pausa/retomada não mudam. Confirmar a mesma faixa preserva o episódio.

Arquivo salvo e auditoria não são transação única: se a gravação posterior
falha, a configuração aplicada é informada como aplicada, com auditoria
pendente em memória. A tarefa de IA retenta armazenamento sem nova chamada.
Queda abrupta pode interromper a auditoria; o próximo início audita o arquivo
carregado. Não há coordenação entre múltiplos processos.

São independentes as instruções de `responder` (personagem antigo),
`analisar_iluminacao` (decisão automática) e `responder_energia` (orientação).
Consultas de expediente, status, registros, parâmetros e custo são resolvidas
por getters locais, em snapshots protegidos; histórico é carregado na partida,
sem leitura de arquivos em cada pergunta. A consulta do último alerta preserva
seu UUID; não atribui custo de outra ocorrência. Custo ativo é parcial, usa
somente duração observada, potência e tarifa locais; ausência é indisponível.

O consultor reutiliza transporte/timeout de Groq/Ollama e valida JSON com
`tipo`/`resposta`. Rejeita conteúdo numérico da LLM, formato inválido e certas
afirmações explícitas não sustentadas. Geral recebe só limites; pergunta sobre
estoque/ambiente recebe snapshot atual com loja, condição, idade, disponibilidade
e expediente, sem credenciais ou envio indiscriminado do histórico. Fora do
escopo é recusado; pedidos de intervenção recebem encaminhamento profissional.
Essas barreiras conservadoras não comprovam qualidade semântica de toda resposta.
Perguntas conceituais como “o que é o LDR?” são encaminhadas ao consultor,
enquanto “como está a iluminação?” continua sendo consulta local. O prompt
também distingue o encerramento do expediente da necessidade contínua de
conservação de produtos refrigerados.

O diálogo ocupa o laço de comandos, sem depender da trava do monitor para
capturar, sintetizar ou consultar IA. A reserva `Voz.dialogo()` adia apenas
novos avisos falados; serial, avaliador e registro seguem. Na saída da reserva,
o aviso é revalidado e cancelado se o episódio terminou. A fila de reprodução
continua única, com exclusão entre microfone e áudio e pausa acústica.

A demonstração física vinculada no README mostra configuração do expediente,
respostas do consultor, claridade/escuro, aviso autônomo com indicação de IA,
LEDs, movimento da cabeça e consulta ao alerta encerrado. Ela não inclui ensaio
de perda de comunicação ou iluminação durante o expediente.

Para repetir as verificações sem hardware, áudio, internet ou chamadas pagas:

```powershell
python -m unittest discover -s tests -v
python maker_lab.py --demonstracao --pasta tmp/ensaio-final
```

A simulação é identificada como tal e mantém custo desconhecido em pendências.
Ela usa pasta separada para preservar o histórico físico e o expediente salvo.
Os testes não medem precisão do LDR, isolamento óptico/acústico ou robustez
mecânica. Esses aspectos exigem ensaios na posição final, com olhos/cabeça em
uso e recuperação após perda do USB. Verificação de compilação não grava
firmware na placa.
