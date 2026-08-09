/**
 * Renders og.png (1200x630) with headless Chrome, using the live numbers from
 * jobs.json so the social card cannot drift from the actual board.
 *
 *   node tools/build-og.mjs
 *
 * No npm dependency: Chrome renders an inline HTML string and screenshots it.
 * Set CHROME_PATH to override binary discovery.
 */

import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  join(process.env.LOCALAPPDATA ?? '', 'Google\\Chrome\\Application\\chrome.exe'),
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
].filter(Boolean)

const chrome = CHROME_CANDIDATES.find((p) => existsSync(p))
if (!chrome) {
  console.error('Could not find Chrome. Set CHROME_PATH to the binary and retry.')
  process.exit(1)
}

const board = JSON.parse(readFileSync(join(root, 'jobs.json'), 'utf8'))
const targets = JSON.parse(readFileSync(join(root, 'tools/targets.json'), 'utf8'))

const openings = board.jobs.reduce((n, j) => n + (j.count ?? 1), 0)
const universities = targets.filter((t) => t.branch === 'university' && t.careers_url).length
const metros = board.locations.filter((l) => !['REMOTE', 'INTL', 'USOTHER'].includes(l.code)).length

/* The portrait is inlined so the render needs no network and the card stays
   reproducible on any machine. */
const portrait = `data:image/jpeg;base64,${readFileSync(
  join(root, 'tools/assets/portrait.jpg'),
).toString('base64')}`

/* Palette mirrors index.html, not a token file: the card should match the page
   someone actually lands on. */
const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    width: 1200px; height: 630px; display: flex; flex-direction: column;
    justify-content: space-between; padding: 58px 76px;
    background: #fbfaf8; color: #1c1b19;
    font-family: "Segoe UI", ui-sans-serif, system-ui, -apple-system, Helvetica, Arial, sans-serif;
    position: relative; overflow: hidden;
  }
  body::after {
    content: ''; position: absolute; right: -200px; top: -200px;
    width: 640px; height: 640px; border-radius: 50%;
    background: radial-gradient(circle, rgba(31,111,92,.10), rgba(31,111,92,0) 70%);
  }
  .top { display: flex; align-items: flex-start; justify-content: space-between; gap: 54px; }
  .copy { flex: 1; min-width: 0; }
  .eyebrow {
    display: flex; align-items: center; gap: 12px;
    font-size: 18px; font-weight: 700; letter-spacing: .16em;
    text-transform: uppercase; color: #1f6f5c;
  }
  .eyebrow i { width: 34px; height: 3px; background: #1f6f5c; border-radius: 2px; }
  h1 {
    font-size: 66px; line-height: 1.05; letter-spacing: -.03em;
    font-weight: 700; margin-top: 20px; white-space: nowrap;
  }
  h1 em { font-style: normal; color: #1f6f5c; }
  .sub { font-size: 27px; color: #1c1b19; margin-top: 20px; line-height: 1.3; }
  .sub2 { font-size: 21px; color: #6d6a63; margin-top: 10px; line-height: 1.3; }
  .sub2 b { color: #1f6f5c; font-weight: 700; }

  .portrait { flex: none; width: 256px; position: relative; z-index: 1; text-align: center; }
  .portrait img {
    width: 244px; height: 244px; border-radius: 50%; object-fit: cover;
    border: 6px solid #fff; box-shadow: 0 10px 34px rgba(28,27,25,.18);
  }
  .tag {
    display: inline-block; margin-top: 16px; padding: 7px 15px; border-radius: 999px;
    background: #1c1b19; color: #fbfaf8; font-size: 13px; font-weight: 700;
    letter-spacing: .1em; text-transform: uppercase; white-space: nowrap;
  }

  .facts { display: flex; gap: 62px; border-top: 2px solid #e6e3dc; padding-top: 22px; }
  .fact dt {
    font-size: 15px; text-transform: uppercase; letter-spacing: .11em;
    color: #6d6a63; font-weight: 700; margin-bottom: 6px;
  }
  .fact dd { font-size: 44px; font-weight: 700; letter-spacing: -.02em; }
</style></head>
<body>
  <div class="top">
    <div class="copy">
      <p class="eyebrow"><i></i>A gentle intervention</p>
      <h1>Soon to be unemployed?<br><em>Absolutely Fucked?</em></h1>
      <p class="sub">I hope this might help a smidge</p>
      <p class="sub2"><b>${openings}</b> open research roles, already filtered</p>
    </div>
    <div class="portrait">
      <img src="${portrait}" alt="">
      <div class="tag">Status: unemployed</div>
    </div>
  </div>

  <dl class="facts">
    <div class="fact"><dt>Open roles</dt><dd>${openings}</dd></div>
    <div class="fact"><dt>Employers</dt><dd>${board.swept}</dd></div>
    <div class="fact"><dt>Universities</dt><dd>${universities}</dd></div>
    <div class="fact"><dt>Metros</dt><dd>${metros}</dd></div>
  </dl>
</body></html>`

const stage = mkdtempSync(join(tmpdir(), 'bridget-og-'))
const page = join(stage, 'og.html')
writeFileSync(page, html)

const out = join(root, 'og.png')

execFileSync(chrome, [
  '--headless',
  '--disable-gpu',
  '--hide-scrollbars',
  '--force-color-profile=srgb',
  '--virtual-time-budget=3000',
  '--window-size=1200,630',
  `--screenshot=${out}`,
  `file://${page.replace(/\\/g, '/')}`,
])

console.log(`Wrote ${out}  (${openings} openings, ${board.swept} employers, ${universities} universities)`)
