// Renders the site's images from the real demo replay: the three step screenshots (WebP),
// the 1200x630 social image and the touch icon. Same needs as make_demo_gif.mjs: Node 18+,
// Playwright with Chromium, and ffmpeg (LC_NODE_MODULES and FFMPEG work the same way).
//
//   node scripts/render_site_assets.mjs [--review <folder>]
//
// --review also saves full page screenshots at 1440 and 375 pixels in both themes.
import { createRequire } from 'node:module'
import { execFileSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, rmSync, statSync } from 'node:fs'
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

const siteDir = join(root, 'site')
const site = pathToFileURL(join(siteDir, 'index.html')).href
const reviewAt = process.argv.indexOf('--review')
const review = reviewAt > 0 ? process.argv[reviewAt + 1] : null
const work = mkdtempSync(join(tmpdir(), 'lc-site-'))

// Each shot ends just after the line that matches, so the terminal shows what led up to it.
const SHOTS = [
  { name: 'step-1', after: /c\d+-\w+\s+bug_fixing/ },
  { name: 'step-2', after: /assertEqual\(pricing\.share/ },
  { name: 'step-3', after: /visitor GET \/checkout -> 200/ },
]

function webp(png, out) {
  execFileSync(ffmpeg, ['-y', '-loglevel', 'error', '-i', png, '-c:v', 'libwebp', '-quality', '88', '-compression_level', '6', out])
}

const browser = await chromium.launch()
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2 })
  await page.goto(site + '?still')
  await page.evaluate(() => document.fonts.ready)
  // A shot is the bare terminal screen at 720x380 CSS pixels, saved at 2x.
  await page.addStyleTag({ content: '.hero-grid { display: block !important } [data-demo] { width: 720px } [data-demo] .screen { height: 380px !important; font-size: 14px; padding: 18px 22px } [data-demo] .window-bar { display: none } [data-demo] .window { border-radius: 0; border: 0 } .js .reveal { opacity: 1; transform: none }' })
  mkdirSync(join(siteDir, 'img'), { recursive: true })
  for (const shot of SHOTS) {
    const at = await page.evaluate((source) => {
      const pattern = new RegExp(source)
      const event = window.LivingCodeDemo.player.plan.events.find((e) => pattern.test(e.text))
      return event ? event.at + 0.05 : null
    }, shot.after.source)
    if (at === null) throw new Error(`no line matches ${shot.after}`)
    await page.evaluate((t) => window.LivingCodeDemo.player.seek(t), at)
    const png = join(work, `${shot.name}.png`)
    await page.locator('[data-demo] .screen').screenshot({ path: png })
    webp(png, join(siteDir, 'img', `${shot.name}.webp`))
  }

  // The social image: real type over the real terminal output.
  const card = await browser.newPage({ viewport: { width: 1200, height: 630 }, deviceScaleFactor: 1 })
  await card.goto(pathToFileURL(join(root, 'scripts', 'og-card.html')).href)
  await card.evaluate(() => document.fonts.ready)
  await card.screenshot({ path: join(siteDir, 'og.png') })

  const icon = await browser.newPage({ viewport: { width: 180, height: 180 }, deviceScaleFactor: 1 })
  await icon.setContent('<body style="margin:0;background:#0D1110"><img src="' + pathToFileURL(join(siteDir, 'favicon.svg')).href + '" style="display:block;width:180px;height:180px"></body>')
  await icon.screenshot({ path: join(siteDir, 'apple-touch-icon.png') })

  if (review) {
    mkdirSync(review, { recursive: true })
    for (const [width, height] of [[1440, 900], [375, 812]]) {
      for (const theme of ['light', 'dark']) {
        const view = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: width < 600 ? 2 : 1, colorScheme: theme })
        await view.goto(site + '?still')
        await view.evaluate(() => document.fonts.ready)
        await view.evaluate(() => {
          document.querySelectorAll('.reveal').forEach((el) => el.classList.add('in'))
          const p = window.LivingCodeDemo.player
          p.seek(p.plan.events.find((e) => /Settled: r0003/.test(e.text)).at + 0.05)
        })
        await view.waitForTimeout(400)
        const name = `${width}-${theme}`
        await view.screenshot({ path: join(review, `${name}.png`), fullPage: true })
        await view.screenshot({ path: join(review, `${name}-fold.png`) })
        const wide = await view.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)
        console.log(`${name}: ${wide ? 'SCROLLS SIDEWAYS' : 'fits the width'}`)
        await view.close()
      }
    }
  }
} finally {
  await browser.close()
  rmSync(work, { recursive: true, force: true })
}

for (const file of ['img/step-1.webp', 'img/step-2.webp', 'img/step-3.webp', 'og.png', 'apple-touch-icon.png']) {
  console.log(`${file}: ${(statSync(join(siteDir, file)).size / 1024).toFixed(0)} KB`)
}
