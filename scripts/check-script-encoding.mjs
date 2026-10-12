#!/usr/bin/env node
/**
 * Guards for Windows script files. Both failures below are silent and confusing, and both are cheap
 * to check — which is the whole point of this script.
 *
 * 1. PowerShell scripts with non-ASCII content must start with a UTF-8 BOM.
 *    Windows PowerShell reads a BOM-less file as ANSI, so Chinese comments become mojibake and the
 *    script fails to parse. This broke `scripts/test-all.ps1` twice.
 *
 * 2. Batch files must use CRLF line endings and must NOT have a BOM.
 *    `cmd.exe` needs CRLF: with LF-only endings the parser slips and eats the first character of
 *    lines (`echo` becomes `cho`), so the launcher dies in a wall of "'cho' is not recognized".
 *    That is exactly how `start-vmaker.bat` broke — an editor rewrote it with LF endings.
 *    A BOM is just as fatal in a .bat: `@echo off` is no longer recognised, so every command gets
 *    echoed to the console.
 *
 * Usage: node scripts/check-script-encoding.mjs
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const BOM = Buffer.from([0xef, 0xbb, 0xbf])
const problems = []
const checked = []
const checkedBats = []

const SKIP = new Set(['node_modules', 'dist', '.git', '.venv'])

function walk(directory) {
  for (const entry of readdirSync(directory)) {
    if (SKIP.has(entry)) continue
    const full = join(directory, entry)
    if (statSync(full).isDirectory()) walk(full)
    else if (entry.endsWith('.ps1')) inspectPowerShell(full)
    else if (entry.toLowerCase().endsWith('.bat')) inspectBatch(full)
  }
}

function relativeOf(path) {
  return path.slice(root.length + 1).replace(/\\/g, '/')
}

function inspectPowerShell(path) {
  const data = readFileSync(path)
  const relative = relativeOf(path)
  const hasBom = data.length >= 3 && data.subarray(0, 3).equals(BOM)
  const body = hasBom ? data.subarray(3) : data
  let nonAscii = false
  for (const byte of body) { if (byte > 0x7f) { nonAscii = true; break } }
  checked.push(relative)
  if (nonAscii && !hasBom) {
    problems.push(`${relative} 含非 ASCII 内容但没有 UTF-8 BOM（Windows PowerShell 会按 ANSI 解码而解析失败）`)
  }
  if (nonAscii && body.toString('utf8').includes('\uFFFD')) {
    problems.push(`${relative} 含非法 UTF-8 字节（已被错误编码破坏）`)
  }
}

function inspectBatch(path) {
  const data = readFileSync(path)
  const relative = relativeOf(path)
  checkedBats.push(relative)

  if (data.length >= 3 && data.subarray(0, 3).equals(BOM)) {
    problems.push(`${relative} 带 UTF-8 BOM：cmd.exe 会识别不了 @echo off，每条命令都会被回显`)
  }

  let crlf = 0
  let lf = 0
  for (let index = 0; index < data.length; index += 1) {
    if (data[index] !== 0x0a) continue
    if (index > 0 && data[index - 1] === 0x0d) crlf += 1
    else lf += 1
  }
  if (lf > 0) {
    problems.push(`${relative} 有 ${lf} 行是 LF 换行：cmd.exe 需要 CRLF，否则会吃掉行首字符（echo → cho）`)
  }
  if (crlf === 0 && lf === 0) problems.push(`${relative} 没有换行符，疑似被写坏`)
  if (data.toString('utf8').includes('\uFFFD')) {
    problems.push(`${relative} 含非法 UTF-8 字节（已被错误编码破坏）`)
  }
}

walk(root)
for (const problem of problems) console.log(`[问题] ${problem}`)
if (problems.length === 0) {
  console.log(`[通过] 脚本编码检查：${checked.length} 个 .ps1（含非 ASCII 的必须带 BOM）、`
    + `${checkedBats.length} 个 .bat（必须 CRLF 且无 BOM）`)
}
process.exit(problems.length ? 1 : 0)
