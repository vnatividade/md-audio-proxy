# Design Brief — Landing "MD para Áudio"

> Inferido do repositório pelo Atelier (concierge — PIP-661) e FECHADO em
> 2026-07-16 com as decisões do fundador: **Direção B "Lampião"** e **CTA único
> "Abrir o app"** (vitrine pessoal, sem caminho "pedir acesso").
> Nada aqui inventa clientes, métricas ou validação: o produto hoje é uma
> ferramenta pessoal com acesso por token.

```yaml
project: md-audio — landing page pública do produto
date: 2026-07-16
product_type: landing
audience: >
  Leitores/estudiosos falantes de pt-BR que escrevem ou recebem documentos
  em Markdown (anotações, capítulos, estudos) e querem ouvi-los em movimento
  (trajeto, caminhada, tarefas domésticas). Uso hoje é pessoal (token);
  mobile-primeiro — o app atual é PWA instalável.
primary_action: Enviar um arquivo .md e ouvir — CTA único "Abrir o app" (rota /app)
direction: >
  B "Lampião" (decisão do fundador, 2026-07-16) — atmosférica: herda o tema do
  app, brilho de abajur, serifa expressiva, onda sonora desenhada pelo scroll.
  Alternativas A e C permanecem como evidência em design/atelier/.
type: { display: Fraunces, body: Instrument Sans }
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

## As três direções (fase 1 — histórico)

| | Direção | Type / Ground / Accent | Postura |
|---|---|---|---|
| A | **Tipográfica-crua** ("Voz") | Anton + Archivo / preto puro #0B0A08 / âmbar #E8A33D em um único ponto | Headline preenchendo a largura, queda de caracteres scroll-driven, lista numerada com réguas. Frieza tipográfica, zero decoração. |
| B | **Atmosférica-lampião** ("Lampião") — **ESCOLHIDA** | Fraunces + Instrument Sans / marrom-negro #14110E / âmbar como luz | Herda o tema do app: brilho de abajur radial, serifa expressiva, forma de onda que se desenha com o scroll. Quente, noturna, íntima. |
| C | **Editorial-papel** ("Manuscrito") | Instrument Serif + Fragment Mono / creme #F5EFE4 / âmbar queimado #B4741A | Metáfora literal documento→áudio: coluna de markdown mono à esquerda vira player à direita; grid com réguas, margens numeradas. Diurna, estruturada. |

Cada arquivo declara type/ground/accent/motion num comentário HTML no topo.

## Decisões fechadas (fundador, 2026-07-16)

1. **Direção B "Lampião"** — a landing continua a identidade do app.
2. **CTA único "Abrir o app"** — vitrine pessoal; sem caminho "pedir acesso".

## Entrega final (fase 2)

- Página: `landing.html` na raiz do repo, servida pelo mesmo Flask (Railway).
- Integração: `/` mostra o app para quem tem acesso (cookie `md_auth`,
  `?token=` ou header) e a landing para visitantes; `/app` sempre mostra o app
  (destino do CTA). Sem mudança para a PWA instalada nem para o fluxo de token.
- Seções: hero (onda scroll-driven) → Do arquivo ao áudio (fila→Mac Studio→mp3)
  → Três vozes, o seu ritmo → Feito para o trajeto (retomada/histórico/PWA/mp3)
  → Um serviço pessoal (nota honesta de token) → rodapé.
- Evidência visual: `design/atelier/screenshots/final/`.
