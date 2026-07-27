'use strict';
// Golden fixtures da transformação (PIP-722 / MD-INTAKE-04).
// Rodar: node --test test/
// Sem dependência nova, sem build step — node:test e node:assert são nativos.
const test = require('node:test');
const assert = require('node:assert/strict');
const { mdaudioTransform } = require('../transform.js');

// Checagem de preservação como asserção: cada token distintivo da entrada
// precisa sobreviver ao texto falado, verbatim. Um teste que nunca falha não
// testa — a suíte 'preservação detecta perda de propósito' abaixo prova que
// esta função pega uma perda de verdade, não só quando tudo já está certo.
function assertPreserved(speakable, tokens) {
  for (const tok of tokens) {
    assert.ok(speakable.includes(tok), `token perdido no texto falado: "${tok}"`);
  }
}

// --- Fixture 1: resposta de agente de IA (cabeçalhos, listas, código, link) ---
test('fixture: resposta de agente de IA', () => {
  const input = `# Resposta do agente

O código abaixo resolve o problema:

\`\`\`python
def soma(a, b):
    return a + b
\`\`\`

Pontos importantes:
- É determinístico
- Não usa rede

Mais detalhes em [documentação oficial](https://docs.example.com/ref).
`;
  const r = mdaudioTransform(input, 'markdown');
  // o bloco de código tem só 2 linhas (<= 3) -> lido literalmente, não omitido
  assert.equal(r.speakable, `Resposta do agente.
pausa de 2 segundos

O código abaixo resolve o problema:

def soma(a, b):, return a + b.

Pontos importantes:

É determinístico.
Não usa rede.

Mais detalhes em [documentação oficial](https://docs.example.com/ref).
`);
  assertPreserved(r.speakable, ['Resposta do agente', 'O código abaixo resolve o problema', 'def soma(a, b)', 'return a + b', 'É determinístico', 'Não usa rede', 'documentação oficial']);
  assert.deepEqual(r.markers, []);
  assert.equal(r.title, 'Resposta do agente');
});

// --- Fixture 2: anotações pessoais, texto cru sem markdown ---
test('fixture: anotações pessoais (texto cru)', () => {
  const input = `Reunião de terça

Discutimos o roadmap do trimestre e alinhamos as prioridades com o time de produto.

Próximos passos

- Enviar proposta ao cliente
- Marcar reunião de follow-up
`;
  const r = mdaudioTransform(input, 'text');
  assert.equal(r.speakable, `Reunião de terça.
pausa de 2 segundos

Discutimos o roadmap do trimestre e alinhamos as prioridades com o time de produto.

Próximos passos.
pausa de 1 segundo

Enviar proposta ao cliente.
Marcar reunião de follow-up.
`);
  assertPreserved(r.speakable, ['Reunião de terça', 'roadmap do trimestre', 'time de produto', 'Próximos passos', 'Enviar proposta ao cliente', 'Marcar reunião de follow-up']);
  assert.equal(r.title, 'Reunião de terça');
});

// --- Fixture 3: citações com atribuição ---
test('fixture: citações com atribuição', () => {
  const input = `> A vida é o que acontece enquanto fazemos outros planos.
— John Lennon

> O único jeito de fazer um bom trabalho é amar o que você faz.
— Steve Jobs
`;
  const r = mdaudioTransform(input, 'markdown');
  assertPreserved(r.speakable, ['vida é o que acontece enquanto fazemos outros planos', 'John Lennon', 'único jeito de fazer um bom trabalho', 'Steve Jobs']);
  assert.equal(r.markers.length, 0);
});

// --- Fixture 4: markdown bagunçado (sem título, hierarquia quebrada, HTML cru) ---
test('fixture: markdown bagunçado (sem título, HTML cru, hierarquia quebrada)', () => {
  const input = `Isso começa sem nenhum cabeçalho, só um parágrafo solto primeiro.

##### Sub-sub-sub-seção direto, sem H1/H2/H3 antes

Texto normal <div class="wrapper"><span>com HTML cru</span></div> misturado no meio.

### Voltando pra um nível "mais alto" depois de um mais fundo
`;
  const r = mdaudioTransform(input, 'markdown');
  // sem cabeçalho no início -> título vem da primeira frase, não de heading
  assert.equal(r.title, 'Isso começa sem nenhum cabeçalho, só um parágrafo solto primeiro.');
  assertPreserved(r.speakable, [
    'Isso começa sem nenhum cabeçalho',
    'Sub-sub-sub-seção direto',
    'com HTML cru',
    'Voltando pra um nível',
  ]);
  assert.ok(!/<[a-zA-Z]/.test(r.speakable), 'HTML cru deveria ter sido removido');
  // hierarquia quebrada (##### antes de qualquer H1-H3) ainda vira pausa de seção — não quebra a transformação
  assert.ok(r.speakable.includes('pausa de 1 segundo'));
});

// --- Fixture 5: conteúdo pesado em tabela e código ---
test('fixture: tabela grande (>3 colunas) e código longo (>3 linhas) omitidos', () => {
  const input = `| Nome | Idade | Cidade | Cargo |
| --- | --- | --- | --- |
| Ana | 30 | SP | Eng |

\`\`\`js
linha1
linha2
linha3
linha4
linha5
\`\`\`
`;
  const r = mdaudioTransform(input, 'markdown');
  assert.ok(r.speakable.includes('[tabela omitida]'));
  assert.ok(r.speakable.includes('[código omitido]'));
  assert.ok(!r.speakable.includes('|'), 'nenhum pipe de tabela deveria sobrar');
  assert.ok(!r.speakable.includes('```'), 'nenhuma cerca de código deveria sobrar (o worker apagaria o marcador junto)');
  assert.ok(!r.speakable.includes('linha1'), 'código omitido não deveria vazar conteúdo');
});

test('fixture: tabela pequena (<=3 colunas) linearizada, não omitida', () => {
  const input = `| Campo | Valor |
| --- | --- |
| nome | Ana |
| idade | 30 |
`;
  const r = mdaudioTransform(input, 'markdown');
  assert.ok(r.speakable.includes('Campo: nome, Valor: Ana'));
  assert.ok(r.speakable.includes('Campo: idade, Valor: 30'));
  assert.ok(!r.speakable.includes('[tabela omitida]'));
});

// --- Fixture 6: pausa escrita pelo usuário, preservada intocada ---
test('fixture: pausa de N segundos escrita pelo usuário é preservada', () => {
  const input = `Primeira parte da fala. pausa de 5 segundos Segunda parte, depois da pausa.

pausa de 1 minuto

Terceira parte, depois de um minuto de silêncio.
`;
  const r = mdaudioTransform(input, 'markdown');
  assertPreserved(r.speakable, ['Primeira parte da fala', 'pausa de 5 segundos', 'Segunda parte', 'pausa de 1 minuto', 'Terceira parte']);
});

// --- Preservação detecta perda de verdade (a suíte não é vazia) ---
test('preservação detecta perda de verdade — prova que o teste não é vazio', () => {
  const input = 'Frase com TOKEN_UNICO_XYZ que precisa sobreviver.';
  const real = mdaudioTransform(input, 'markdown');
  assertPreserved(real.speakable, ['TOKEN_UNICO_XYZ']); // passa com a transformação real

  // transformação quebrada de propósito: descarta o conteúdo inteiro
  const broken = { speakable: '' };
  assert.throws(() => assertPreserved(broken.speakable, ['TOKEN_UNICO_XYZ']), /token perdido/);
});

// --- Validade: entrada que só sobra com marcador de omissão é sinalizada ---
test('validade: entrada que reduz só a marcador de omissão é sinalizada como vazia', () => {
  const input = '```\nlinha1\nlinha2\nlinha3\nlinha4\n```\n';
  const r = mdaudioTransform(input, 'markdown');
  assert.equal(r.empty, true);
});

test('validade: conteúdo real nunca é sinalizado como vazio', () => {
  const r = mdaudioTransform('Isso é uma frase normal com conteúdo de verdade.', 'markdown');
  assert.equal(r.empty, false);
});
