# coopbot — jogos co-op no Discord

Bot em Python que roda no GitHub Actions (sem servidor) e posta no Discord via webhooks.

| Canal | Secret | O que posta | Quando (Brasília) |
|---|---|---|---|
| #gratis | `WEBHOOK_GRATIS` | Grátis da Epic; jogos 100% off na Steam | Epic: quinta ~13h17 (reforço 15h47). Steam: diário 09h37 |
| #promo-coop | `WEBHOOK_PROMO` | Resumo diário de co-op em promoção (🔥 = menor preço histórico) | diário 09h37 |
| #lancamentos-coop | `WEBHOOK_LANCAMENTOS` | Co-op/crossplay lançados recentemente ou prestes a lançar | diário 09h37 |
| #votacao | `WEBHOOK_VOTACAO` | 3–5 sugestões numeradas + reações | sexta 18h13 |
| #log-bot | `WEBHOOK_LOG` | Erros e avisos | quando necessário |

## Instalação

1. Crie um repositório no GitHub e suba estes arquivos (`coopbot.py`, `config.yaml`, `state.json`,
   `requirements.txt`, `.github/workflows/coopbot.yml`).
2. Em cada canal do Discord: *Editar canal → Integrações → Webhooks → Novo webhook → Copiar URL*.
3. No repositório: *Settings → Secrets and variables → Actions → New repository secret*, um para cada
   nome da tabela acima.
4. Para as **reações numeradas** da votação (veja a nota abaixo), crie também o secret `DISCORD_BOT_TOKEN`.
5. Em *Actions → coopbot → Run workflow*, rode com `dry_run` marcado para ver as mensagens no log,
   depois sem `dry_run` para postar de verdade.

> **Reações na votação:** webhooks do Discord *não conseguem* adicionar reações. Para que o bot as
> adicione sozinho, crie um aplicativo em <https://discord.com/developers/applications>, gere o token do
> bot, convide-o ao servidor com as permissões *Ver canal*, *Ler histórico* e *Adicionar reações*, e
> salve o token no secret `DISCORD_BOT_TOKEN`. Sem ele a votação é postada normalmente e o `#log-bot`
> avisa que faltaram as reações (dá para reagir à mão).

## Testar localmente

```powershell
pip install -r requirements.txt
python coopbot.py promo --dry-run          # imprime a mensagem, não envia nada nem grava estado
python coopbot.py all --dry-run -v         # todos os jobs; -v mostra por que cada jogo foi descartado
```

`--dry-run` ignora as janelas de dia/hora (Epic só na quinta ≥16h UTC, votação só na sexta).
Fora do dry-run use `--force` para o mesmo efeito.

## Configuração (`config.yaml`)

Jogadores mínimos, avaliação mínima, desconto mínimo, tamanho do resumo, janelas de lançamento etc.
`avaliacao_minima` aceita nomes em inglês ou pt-BR da escala da Steam (Extremamente positivas → Muito
positivas → Positivas → Majoritariamente positivas → Mistas…). "Ligeiramente positivas" vale como
*Majoritariamente positivas*.

## Como funciona (e limitações que vale conhecer)

- **Filtro co-op:** `appdetails` da Steam (`l=english`, por ID de categoria): 9 Co-op, 38 Online Co-op,
  39 Split-screen co-op → tags 🤝 / 🛋️; 27 Cross-Platform → 🔀; 44 Remote Play Together → 🛋️.
- **Nº de jogadores:** a Steam **não fornece** esse dado por API. O bot o deduz da descrição do jogo
  (“up to 4 players”, “2-4 jogadores”). Quando a descrição não diz, o jogo passa ou não conforme
  `jogadores_desconhecido`. Use `jogadores_override` (appid → nº) para corrigir casos específicos.
- **Descoberta das promoções:** a busca da Steam já filtrada por categoria co-op + em promoção;
  CheapShark entra para o **menor preço histórico** 🔥 (USD, comparado com o preço atual da Steam).
  O preço exibido é em reais, vindo da própria Steam. Só lojas Steam são consideradas.
- **Avaliações:** `/appreviews` da Steam anda redirecionando; o bot cai para o `appreviewhistogram`,
  que é ligeiramente menos exato (diferença de ~1 ponto percentual nos testes).
- **Estado:** `state.json` é commitado pelo próprio workflow. Promoções só se repetem após
  `repetir_apos_dias`; grátis e lançamentos não se repetem.
- **GitHub desativa workflows agendados** em repositórios sem atividade por 60 dias. Os commits do
  estado são feitos pelo próprio bot e podem não contar; se parar de rodar, reative em *Actions*.
- Os horários dos crons podem atrasar alguns minutos (normal no GitHub Actions).
