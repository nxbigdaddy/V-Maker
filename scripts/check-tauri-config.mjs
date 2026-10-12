#!/usr/bin/env node
/**
 * Static checks for the Tauri desktop configuration.
 *
 * A full `tauri build` needs Rust **and** the MSVC build tools, neither of which is available here,
 * so the shell was previously "configured but never checked". Everything that can be verified
 * without a compiler is verified here instead: the JSON parses, every referenced path exists, the
 * hook commands point at real package scripts, and the version/identifier fields are coherent.
 *
 * This does NOT prove the desktop app builds or runs. It proves the configuration is not already
 * broken in ways that would only show up minutes into a build.
 *
 * Usage: node scripts/check-tauri-config.mjs
 */
import { readFileSync, existsSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const problems = []
const notes = []

function fail(message) { problems.push(message) }
function note(message) { notes.push(message) }

function readJson(path, label) {
  if (!existsSync(path)) { fail(`${label} 不存在: ${path}`); return null }
  try {
    return JSON.parse(readFileSync(path, 'utf8'))
  } catch (error) {
    fail(`${label} 不是合法 JSON: ${error.message}`)
    return null
  }
}

const configPath = join(root, 'src-tauri', 'tauri.conf.json')
const cargoPath = join(root, 'src-tauri', 'Cargo.toml')
const packagePath = join(root, 'package.json')

const config = readJson(configPath, 'tauri.conf.json')
const packageJson = readJson(packagePath, 'package.json')
if (!config || !packageJson) { report(); process.exit(1) }

// ---- 版本与标识 ----
if (config.version !== packageJson.version) {
  fail(`版本不一致：tauri.conf.json=${config.version} package.json=${packageJson.version}`)
}
const cargo = existsSync(cargoPath) ? readFileSync(cargoPath, 'utf8') : ''
const cargoVersion = /^version\s*=\s*"([^"]+)"/m.exec(cargo)?.[1]
if (!cargoVersion) fail('Cargo.toml 缺少 version')
else if (cargoVersion !== config.version) {
  fail(`版本不一致：Cargo.toml=${cargoVersion} tauri.conf.json=${config.version}`)
}
if (!/^[A-Za-z0-9.-]+$/.test(config.identifier ?? '')) {
  fail(`identifier 只能是反向域名（字母/数字/点/连字符）：${config.identifier}`)
}

// ---- 构建钩子与产物目录 ----
const scripts = packageJson.scripts ?? {}
for (const key of ['beforeDevCommand', 'beforeBuildCommand']) {
  const command = config.build?.[key]
  if (!command) { fail(`build.${key} 未配置`); continue }
  const script = String(command).trim().split(/\s+/).at(-1)
  if (!(script in scripts)) fail(`build.${key} 调用了不存在的 npm script: ${script}`)
}
const dist = config.build?.frontendDist
if (!dist) fail('build.frontendDist 未配置')
else {
  const distPath = resolve(dirname(configPath), dist)
  if (!existsSync(distPath)) fail(`frontendDist 指向的目录不存在: ${distPath}（先跑一次 pnpm build:web）`)
  else if (!existsSync(join(distPath, 'index.html'))) fail(`frontendDist 里没有 index.html: ${distPath}`)
}

// ---- 图标：空数组会让打包阶段才失败 ----
const icons = config.bundle?.icon
if (!Array.isArray(icons) || icons.length === 0) {
  fail('bundle.icon 为空：打包时才会报错，必须先提供图标（可用 pnpm tauri icon <png>）')
} else {
  for (const icon of icons) {
    const iconPath = join(root, 'src-tauri', icon)
    if (!existsSync(iconPath)) fail(`bundle.icon 引用的文件不存在: ${icon}`)
    else if (statSync(iconPath).size === 0) fail(`bundle.icon 引用的文件是空的: ${icon}`)
  }
  const names = icons.map((icon) => icon.split('/').pop())
  if (!names.includes('icon.ico')) fail('Windows 打包需要 icons/icon.ico')
}

// ---- 窗口与权限 ----
const windows = config.app?.windows
if (!Array.isArray(windows) || windows.length === 0) fail('app.windows 未配置')
else {
  for (const [index, window] of windows.entries()) {
    const label = window.label ?? 'main'
    if (window.width && window.minWidth && window.minWidth > window.width) {
      fail(`窗口 ${index}（${label}）的 minWidth 大于 width`)
    }
    if (window.height && window.minHeight && window.minHeight > window.height) {
      fail(`窗口 ${index}（${label}）的 minHeight 大于 height`)
    }
  }
}
const capabilityPath = join(root, 'src-tauri', 'capabilities', 'default.json')
const capability = readJson(capabilityPath, 'capabilities/default.json')
if (capability) {
  const labels = new Set((windows ?? []).map((window) => window.label ?? 'main'))
  for (const target of capability.windows ?? []) {
    if (!labels.has(target)) fail(`capabilities 指向不存在的窗口标签: ${target}`)
  }
  if (!capability.permissions?.includes('core:default')) {
    fail('capabilities 缺少 core:default，窗口将无法使用核心接口')
  }
}

// ---- 已知限制（不影响配置正确性，但必须让人看见）----
const jsPluginsUsed = ['@tauri-apps/api', '@tauri-apps/plugin-dialog', '@tauri-apps/plugin-opener', '@tauri-apps/plugin-shell']
const missing = jsPluginsUsed.filter((name) => !existsSync(join(root, 'node_modules', name)))
if (missing.length) {
  note(`Rust 侧初始化了插件，但前端没有装也没有调用它们：${missing.join(', ')}`)
  note('本应用是「Web UI + 本地 HTTP 后端」，前端不依赖 Tauri API；插件目前不产生功能。')
}
if (!existsSync(join(root, 'src-tauri', 'gen'))) {
  note('src-tauri/gen（capabilities 的 $schema）要等第一次 cargo 构建才会生成，现在缺失属正常。')
}

function report() {
  for (const item of notes) console.log(`[提示] ${item}`)
  if (problems.length === 0) {
    console.log('[通过] Tauri 配置静态检查：引用的文件、脚本与版本都自洽')
    console.log('[边界] 这不代表桌面应用能构建——本机没有 Rust 与 MSVC 生成工具，打包从未验证过。')
  } else {
    for (const item of problems) console.log(`[问题] ${item}`)
  }
}

report()
process.exit(problems.length ? 1 : 0)
