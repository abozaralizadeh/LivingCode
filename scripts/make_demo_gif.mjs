// Turns the site's replay of a real demo run into docs/media/demo.gif for the README.
//
// Needs Node 18+, Playwright with Chromium, and ffmpeg. If Playwright and ffmpeg-static live in
// another folder's node_modules, point LC_NODE_MODULES at that folder. FFMPEG can point at an
// ffmpeg binary. Run scripts/record_demo.py first if the demo output has changed.
//
//   node scripts/make_demo_gif.mjs [--shots <folder>]
//
// --shots also saves desktop and phone screenshots of the whole site, to check the layout.
import { createRequire } from 'node:module'
import { execFileSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const modules = process.env.LC_NODE_MODULES
const require = createRequire(modules ? join(modules, 'package.json') : import.meta.url)
const { chromium } = require('playwright')
let ffmpeg = process.env.FFMPEG || 'ffmpeg'
if (!process.env.FFMPEG) {
  try { ffmpeg = require('ffmpeg-static') || ffmpeg } catch { /* use ffmpeg from PATH */ }
}

const FPS = 10
const site = pathToFileURL(join(root, 'site', 'index.html')).href
const output = join(root, 'docs', 'media', 'demo.gif')
const shotsAt = process.argv.indexOf('--shots')
const shots = shotsAt > 0 ? process.argv[shotsAt + 1] : null

const browser = await chromium.launch()
try {
  if (shots) {
    mkdirSync(shots, { recursive: true })
    for (const [name, viewport, scale] of [['desktop', { width: 1280, height: 900 }, 1], ['phone', { width: 390, height: 844 }, 2]]) {
      const page = await browser.newPage({ viewport, deviceScaleFactor: scale })
      await page.goto(site + '?still')
      await page.evaluate(() => { const p = window.LivingCodeDemo.player; p.seek(p.plan.length) })
      await page.screenshot({ path: join(shots, `${name}.png`), fullPage: true })
      const wide = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)
      console.log(`${name}: ${wide ? 'scrolls sideways' : 'fits the width'}`)
      await page.close()
    }
  }

  const page = await browser.newPage({ viewport: { width: 960, height: 800 }, deviceScaleFactor: 1 })
  await page.goto(site + '?still')
  await page.addStyleTag({ content: '[data-replay] { display: none } [data-demo] { padding: 16px; background: var(--bg) }' })
  const box = page.locator('[data-demo]')
  const length = await page.evaluate(() => window.LivingCodeDemo.player.plan.length)
  const frames = mkdtempSync(join(tmpdir(), 'lc-gif-'))
  const shown = []
  let last = null
  for (let step = 0; step <= Math.ceil(length * FPS); step++) {
    const t = step / FPS
    const key = await page.evaluate((at) => window.LivingCodeDemo.player.seek(at), t)
    if (key === last) continue
    last = key
    const file = join(frames, `f${String(shown.length).padStart(5, '0')}.png`)
    await box.screenshot({ path: file })
    shown.push({ file, t })
  }
  const list = shown.map((frame, i) => {
    const next = i + 1 < shown.length ? shown[i + 1].t : length
    return `file '${frame.file}'\nduration ${(next - frame.t).toFixed(2)}`
  })
  list.push(`file '${shown[shown.length - 1].file}'`)
  writeFileSync(join(frames, 'list.txt'), list.join('\n') + '\n')
  mkdirSync(dirname(output), { recursive: true })
  execFileSync(ffmpeg, [
    '-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', join(frames, 'list.txt'),
    '-vf', 'split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle',
    '-fps_mode', 'vfr', '-loop', '0', output,
  ])
  rmSync(frames, { recursive: true, force: true })
  const size = statSync(output).size / 1024 / 1024
  console.log(`Saved ${shown.length} frames, ${length.toFixed(0)} seconds, ${size.toFixed(1)} MB to docs/media/demo.gif`)
} finally {
  await browser.close()
}
