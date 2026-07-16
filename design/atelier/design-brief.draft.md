# Design Brief (draft) — Landing "MD para Áudio"

> Rascunho inferido do repositório pelo Atelier (concierge, fase 1 — PIP-661).
> Nada aqui inventa clientes, métricas ou validação: o produto hoje é uma
> ferramenta pessoal com acesso por token. As perguntas abertas estão no fim.

```yaml
project: md-audio — landing page pública do produto
date: 2026-07-16
product_type: landing
audience: >
  Leitores/estudiosos falantes de pt-BR que escrevem ou recebem documentos
  em Markdown (anotações, capítulos, estudos) e querem ouvi-los em movimento
  (trajeto, caminhada, tarefas domésticas). Uso hoje é pessoal (token);
  mobile-primeiro — o app atual é PWA instalável.
primary_action: Enviar um arquivo .md e ouvir — CTA único "Abrir o app" (rota /)
direction: A ESCOLHER — três direções renderizadas em design/atelier/direction-{a,b,c}.html
type:
  direction-a: { display: Anton, body: Archivo }
  direction-b: { display: Fraunces, body: Instrument Sans }
  direction-c: { display: Instrument Serif, body: Instrument Sans, data: Fragment Mono }
color:
  seeds-da-marca:  # tokens já existentes no app "Meus Áudios" (tema Lampião)
    ground-escuro: "#14110E"
    ground-claro: "#F5EFE4"
    accent-escuro: "#E8A33D"
    accent-claro: "#B4741A"
    texto-escuro: "#EFE7DA"
  semantic: default
motion: subtle  # reveals por IntersectionObserver + um toque scroll-driven por direção; prefers-reduced-motion respeitado
stack: vanilla  # Flask serve HTML inline; single-file HTML é o formato natural deste repo
constraints:
  - single-file HTML (CSS/JS inline), sem build step — padrão do repo (app.py serve string)
  - Google Fonts como única dependência externa permitida
  - o app real fica na rota / com token; a landing não pode prometer signup aberto
  - copy 100% pt-BR, sem lorem
anti:
  - slop list da casa (gradiente roxo, Inter como display, cards sombreados uniformes, emoji utilitário)
  - NÃO inventar depoimentos, número de usuários, métricas ou logos de clientes
  - NÃO prometer "privacidade total" — o texto passa pela fila no Railway antes do Mac Studio
```

## O que é verdade sobre o produto (fonte: app.py, commits)

- Converte arquivo Markdown (.md, até 2 MB) em áudio mp3 em português brasileiro.
- TTS Kokoro no Mac Studio pessoal; a web (Railway) é só fila/fachada.
- 3 vozes: Santa e Alex (masculinas), Dora (feminina); prévia de voz antes de gerar.
- Velocidade da fala 0,8×–1,3×; reprodução 1×/1,25×/1,5×/2×; pular ±15s.
- Retoma de onde parou; histórico no aparelho (fixar, renomear, buscar); salva o .mp3.
- Comando escrito de pausa: "pausa de 5 segundos" / "pausa de 1 minuto" vira silêncio.
- PWA instalável ("Meus Áudios"); acesso por token de dispositivo.

## Estado atual da UI

Não existe landing: a rota / já é o formulário do app (card central 440px,
tema "Lampião" claro/escuro, serifa de sistema Iowan Old Style, accent âmbar).
Funcional e honesto, mas não apresenta o produto a quem chega de fora.

## As três direções (reaja, não descreva)

| | Direção | Type / Ground / Accent | Postura |
|---|---|---|---|
| A | **Tipográfica-crua** ("Voz") | Anton + Archivo / preto puro #0B0A08 / âmbar #E8A33D em um único ponto | Headline preenchendo a largura, queda de caracteres scroll-driven, lista numerada com réguas. Frieza tipográfica, zero decoração. |
| B | **Atmosférica-lampião** ("Lampião") | Fraunces + Instrument Sans / marrom-negro #14110E / âmbar como luz | Herda o tema do app: brilho de abajur radial, serifa expressiva, forma de onda que se desenha com o scroll. Quente, noturna, íntima. |
| C | **Editorial-papel** ("Manuscrito") | Instrument Serif + Fragment Mono / creme #F5EFE4 / âmbar queimado #B4741A | Metáfora literal documento→áudio: coluna de markdown mono à esquerda vira player à direita; grid com réguas, margens numeradas. Diurna, estruturada. |

Cada arquivo declara type/ground/accent/motion num comentário HTML no topo.

## Perguntas de gosto para o fundador (as únicas decisões pendentes)

1. **Qual direção — ou qual mistura?** (ex.: "B com a tipografia da A"). A landing
   deve continuar a identidade "Lampião" do app (B/C herdam; A rompe de propósito)?
2. **A landing é vitrine pessoal ou porta de entrada?** Hoje o acesso é por token.
   O CTA único "Abrir o app" está certo, ou você quer um caminho "pedir acesso"
   (mailto/WhatsApp)? Isso muda a força do CTA nas três direções.
