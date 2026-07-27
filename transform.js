// ---------------------------------------------------------------------------
// Colar e Ouvir — transformação determinística p/ markdown falável (PIP-721).
// Contrato: product/ventures/md-audio/audiobook-markdown-profile-draft.md
//
// O worker do Mac Studio roda a própria markdown_to_text() DEPOIS deste
// código (PIP-719) e é segura para #, >, listas, **, [texto](url), `código` —
// isso fica para o worker. Mas é destrutiva ou ausente para: cerca de código
// (apaga tudo sem marcador), imagem sem alt (colapsa sem marcador), tabela
// (vira ", " sem sentido), régua horizontal (some sem pausa), HTML cru e
// footnote (nenhum tratamento), URL crua (não detectada). Resolvido aqui,
// antes de qualquer coisa chegar ao worker.
// ---------------------------------------------------------------------------
const MDA_PAUSE_CHAPTER = 'pausa de 2 segundos';
const MDA_PAUSE_SECTION = 'pausa de 1 segundo';
const MDA_PAUSE_HR = 'pausa de 3 segundos';
const MDA_MARK_CODE = '[código omitido]';
const MDA_MARK_LINK = '[link omitido]';
const MDA_MARK_IMAGE = '[imagem omitida]';
const MDA_MARK_TABLE = '[tabela omitida]';
const MDA_CODE_FENCE_MAX_LINES = 3;
const MDA_TABLE_MAX_COLS = 3;
const MDA_TITLE_MAX_LEN = 80;
const MDA_PLAIN_HEADING_MAX_LEN = 60;

// ':' conta como pontuação terminal o bastante — evita "texto:." quando a
// frase introduz lista/código/tabela que já carrega sua própria pausa.
function mdaTerminalOk(s) { return /[.!?:]["')\]]?\s*$/.test(s); }
function mdaEnsureTerminal(s) {
  const t = (s || '').replace(/\s+$/, '');
  return !t || mdaTerminalOk(t) ? t : t + '.';
}

// Cercas de código — extraídas primeiro sempre: o worker apaga o conteúdo
// inteiro (marcador incluso) se sobrar dentro de ```, então nunca reemitir ```.
function mdaResolveCodeFences(text) {
  const markers = [];
  const out = text.replace(/```[^\n]*\n([\s\S]*?)```[ \t]*\n?/g, (whole, body) => {
    const lines = body.replace(/\n$/, '').split('\n').map(l => l.trim()).filter(Boolean);
    if (lines.length > 0 && lines.length <= MDA_CODE_FENCE_MAX_LINES) {
      return mdaEnsureTerminal(lines.join(', ')) + '\n\n';
    }
    markers.push('código');
    return MDA_MARK_CODE + '\n\n';
  });
  return { text: out, markers };
}

function mdaStripRawHtml(text) {
  return text.replace(/<\/?[a-zA-Z][a-zA-Z0-9-]*(?:\s[^<>]*)?>/g, '');
}

// Imagens: o worker resolve ![alt](url) -> alt sozinho, mas colapsa alt vazio
// pra nada (perda silenciosa) e não usa o prefixo "Imagem:" do contrato.
function mdaResolveImages(text) {
  const markers = [];
  const out = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, (whole, alt) => {
    const a = alt.trim();
    if (!a) { markers.push('imagem'); return MDA_MARK_IMAGE; }
    return mdaEnsureTerminal('Imagem: ' + a);
  });
  return { text: out, markers };
}

// URL crua fora de [texto](url) — o worker não detecta isso.
function mdaResolveBareUrls(text) {
  const markers = [];
  const out = text.replace(/(\]\()?\bhttps?:\/\/[^\s)]+/g, (whole, precededByParen) => {
    if (precededByParen) return whole; // URL de [texto](url) válido — fica pro worker
    markers.push('link');
    return MDA_MARK_LINK;
  });
  return { text: out, markers };
}

// Footnotes: worker não trata nada. Referência [^n] inline é removida;
// definição [^n]: texto é guardada e devolvida no fim da seção corrente.
function mdaExtractFootnoteDefs(lines) {
  const defs = new Map();
  const kept = [];
  const defRe = /^\[\^([^\]]+)\]:\s*(.*)$/;
  for (const line of lines) {
    const m = defRe.exec(line);
    if (m) { defs.set(m[1], m[2].trim()); continue; }
    kept.push(line);
  }
  return { lines: kept, defs };
}
function mdaStripFootnoteRefs(text) { return text.replace(/\[\^[^\]]+\]/g, ''); }

// Régua horizontal: deixada como `---`, o worker apaga a linha inteira sem
// pausa. Precisa virar o comando de pausa em texto literal.
function mdaIsHR(line) { return /^\s*([-*_])\s*(?:\1\s*){2,}$/.test(line); }

// Tabela: o worker só tira a separadora e troca `|` por ", " — sem sentido
// falado. Resolvida inteiramente aqui; nenhum `|` sobra para o worker.
function mdaIsTableSep(line) { return /^\s*\|?[\s:|-]+\|?\s*$/.test(line) && /-/.test(line); }
function mdaSplitRow(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|')) s = s.slice(0, -1);
  return s.split('|').map(c => c.trim());
}
function mdaResolveTable(headerLine, dataLines) {
  const header = mdaSplitRow(headerLine);
  if (header.length > MDA_TABLE_MAX_COLS) return { text: MDA_MARK_TABLE + '\n', omitted: true };
  const rows = dataLines.map(mdaSplitRow);
  const sentences = rows.map(row =>
    mdaEnsureTerminal(header.map((h, i) => h + ': ' + (row[i] || '')).join(', '))
  );
  return { text: sentences.join('\n') + '\n', omitted: false };
}

// Listas: o worker tira o marcador em qualquer indentação e não separa
// frase nenhuma — nenhuma profundidade de nesting sobrevive nele de qualquer
// jeito, então cada item vira uma frase falada própria, achatada aqui mesmo.
function mdaIsListItem(line) { return /^\s*(?:[-*+]|\d+\.)\s+/.test(line); }
function mdaListItemText(line) { return line.replace(/^\s*(?:[-*+]|\d+\.)\s+/, ''); }

// Cabeçalhos: resolvidos aqui (não deixados como # para o worker) porque
// precisamos inserir o comando de pausa de estrutura logo depois em texto
// literal — nunca dentro de sintaxe que o worker possa engolir junto.
function mdaHeadingMatch(line) {
  const m = /^(#{1,6})\s+(.*)$/.exec(line);
  return m ? { level: m[1].length, text: m[2].trim() } : null;
}

function mdaInferTitle(text) {
  const firstLine = text.split('\n').find(l => l.trim());
  if (!firstLine) return null;
  const s = firstLine.trim();
  return s.length > 0 && s.length <= MDA_TITLE_MAX_LEN ? s : null;
}

// Modo Markdown cru.
function mdaTransformMarkdown(raw) {
  const markers = [];
  let text = raw.replace(/\r\n?/g, '\n');
  const fenced = mdaResolveCodeFences(text); text = fenced.text; markers.push(...fenced.markers);
  text = mdaStripRawHtml(text);
  const imgs = mdaResolveImages(text); text = imgs.text; markers.push(...imgs.markers);
  const urls = mdaResolveBareUrls(text); text = urls.text; markers.push(...urls.markers);

  let lines = text.split('\n');
  const fn = mdaExtractFootnoteDefs(lines);
  lines = fn.lines;
  const footnoteDefs = fn.defs;

  const out = [];
  let title = null;
  let pendingFootnotes = [];
  function flushFootnotes() {
    for (const [, body] of pendingFootnotes) { out.push(mdaEnsureTerminal(mdaStripFootnoteRefs(body))); out.push(''); }
    pendingFootnotes = [];
  }
  function refsIn(s) {
    const refs = []; const re = /\[\^([^\]]+)\]/g; let m;
    while ((m = re.exec(s))) { if (footnoteDefs.has(m[1])) refs.push(m[1]); }
    return refs;
  }

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }

    if (mdaIsHR(line)) { out.push(MDA_PAUSE_HR); out.push(''); i++; continue; }

    const h = mdaHeadingMatch(line);
    if (h) {
      for (const ref of refsIn(h.text)) pendingFootnotes.push([ref, footnoteDefs.get(ref)]);
      const rawHeading = mdaStripFootnoteRefs(h.text).trim();
      if (title === null) title = mdaInferTitle(rawHeading) || rawHeading.slice(0, MDA_TITLE_MAX_LEN);
      out.push(mdaEnsureTerminal(rawHeading));
      out.push(h.level <= 2 ? MDA_PAUSE_CHAPTER : MDA_PAUSE_SECTION);
      out.push('');
      flushFootnotes();
      i++; continue;
    }

    if (i + 1 < lines.length && mdaIsTableSep(lines[i + 1]) && line.includes('|')) {
      let j = i + 2; const dataLines = [];
      while (j < lines.length && lines[j].trim() && lines[j].includes('|')) { dataLines.push(lines[j]); j++; }
      const resolved = mdaResolveTable(line, dataLines);
      out.push(resolved.text.replace(/\n$/, '')); out.push('');
      if (resolved.omitted) markers.push('tabela');
      i = j; continue;
    }

    if (/^\s{0,3}>/.test(line)) {
      const quoteLines = []; let j = i;
      while (j < lines.length && /^\s{0,3}>/.test(lines[j])) { quoteLines.push(lines[j].replace(/^\s{0,3}>\s?/, '')); j++; }
      let attribution = null;
      if (j < lines.length && lines[j].trim() && /^\s*[—-]{1,2}\s*\S/.test(lines[j])) { attribution = lines[j].trim(); j++; }
      out.push(quoteLines.map(l => '> ' + l).join('\n'));
      if (attribution) out.push(mdaEnsureTerminal(attribution));
      out.push('');
      i = j; continue;
    }

    if (mdaIsListItem(line)) {
      let j = i;
      while (j < lines.length && mdaIsListItem(lines[j])) {
        for (const ref of refsIn(lines[j])) pendingFootnotes.push([ref, footnoteDefs.get(ref)]);
        out.push(mdaEnsureTerminal(mdaStripFootnoteRefs(mdaListItemText(lines[j]))));
        j++;
      }
      out.push(''); i = j; continue;
    }

    {
      const paraLines = []; let j = i;
      while (
        j < lines.length && lines[j].trim() &&
        !mdaHeadingMatch(lines[j]) && !mdaIsHR(lines[j]) && !mdaIsListItem(lines[j]) &&
        !/^\s{0,3}>/.test(lines[j]) &&
        !(j + 1 < lines.length && mdaIsTableSep(lines[j + 1]))
      ) { paraLines.push(lines[j]); j++; }
      const para = paraLines.join(' ').trim();
      if (para) {
        for (const ref of refsIn(para)) pendingFootnotes.push([ref, footnoteDefs.get(ref)]);
        const rawPara = mdaStripFootnoteRefs(para).trim();
        if (title === null) title = mdaInferTitle(rawPara);
        out.push(mdaEnsureTerminal(rawPara)); out.push('');
      }
      i = j === i ? i + 1 : j; // salvaguarda contra loop infinito
    }
  }
  flushFootnotes();
  const speakable = out.join('\n').replace(/\n{3,}/g, '\n\n').trim() + '\n';
  return { speakable, title, markers };
}

// Modo Texto cru — sem sintaxe markdown; estrutura pela forma do próprio
// texto. Nada além do especificado abaixo é inferido (na dúvida, parágrafo).
function mdaTransformPlainText(raw) {
  let text = raw.replace(/\r\n?/g, '\n');
  text = mdaStripRawHtml(text);
  const urls = mdaResolveBareUrls(text); text = urls.text; const markers = urls.markers;

  const blocks = text.split(/\n{2,}/).map(b => b.trim()).filter(Boolean);
  const out = []; let title = null; let firstBlock = true;

  for (const block of blocks) {
    const lines = block.split('\n').map(l => l.trim()).filter(Boolean);
    if (lines.length === 0) continue;

    if (lines.every(mdaIsListItem)) {
      for (const l of lines) out.push(mdaEnsureTerminal(mdaListItemText(l)));
      out.push(''); firstBlock = false; continue;
    }

    const isPlainHeading = lines.length === 1 && lines[0].length <= MDA_PLAIN_HEADING_MAX_LEN && !mdaTerminalOk(lines[0]);
    if (isPlainHeading) {
      if (title === null) title = mdaInferTitle(lines[0]) || lines[0];
      out.push(mdaEnsureTerminal(lines[0]));
      out.push(firstBlock ? MDA_PAUSE_CHAPTER : MDA_PAUSE_SECTION);
      out.push(''); firstBlock = false; continue;
    }

    const rawPara = lines.join(' ').trim();
    if (title === null) title = mdaInferTitle(rawPara);
    out.push(mdaEnsureTerminal(rawPara)); out.push('');
    firstBlock = false;
  }
  const speakable = out.join('\n').replace(/\n{3,}/g, '\n\n').trim() + '\n';
  return { speakable, title, markers };
}

// Regra de validade do profile: entrada que se reduz só a marcadores de
// omissão é erro que o preview deve declarar — não conteúdo real.
function mdaIsOnlyMarkers(speakable) {
  const stripped = speakable
    .replace(/\[código omitido\]/g, '').replace(/\[link omitido\]/g, '')
    .replace(/\[imagem omitida\]/g, '').replace(/\[tabela omitida\]/g, '')
    .replace(/pausa de [\d.,]+\s*(?:minutos?|mins?|min|m|segundos?|segs?|seg|s)\b/gi, '')
    .replace(/[\s.!?,:;>-]+/g, '');
  return stripped.length === 0;
}

// Ponto de entrada único. mode: 'markdown' | 'text'.
function mdaudioTransform(raw, mode) {
  const input = (raw || '');
  if (!input.trim()) return { speakable: '', title: null, markers: [], empty: true };
  const result = mode === 'text' ? mdaTransformPlainText(input) : mdaTransformMarkdown(input);
  result.empty = !result.speakable.trim() || mdaIsOnlyMarkers(result.speakable);
  return result;
}

// Exporta pro Node (testes, node --test) sem afetar o uso direto no browser —
// `module` não existe lá, então este bloco nunca roda no client.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { mdaudioTransform, mdaTransformMarkdown, mdaTransformPlainText, mdaEnsureTerminal };
}
