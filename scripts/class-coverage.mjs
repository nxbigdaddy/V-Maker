/**
 * 列出 src/**\/*.tsx 里实际用到的所有 CSS 类名，并在样式表中检查覆盖情况。
 *
 * 用法：
 *   node scripts/class-coverage.mjs            # 报告未被任何规则提到的类名
 *   node scripts/class-coverage.mjs --list     # 只列出所有类名
 *
 * 重构样式表时用它兜底，避免某个组件的类名被漏掉而「掉样式」。
 */
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

const root = 'src'

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    return statSync(path).isDirectory() ? walk(path) : [path]
  })
}

const files = walk(root).filter((path) => path.endsWith('.tsx') || path.endsWith('.ts'))
const used = new Map()

const addAll = (value, file) => {
  for (const token of value.split(/\s+/)) {
    const name = token.trim()
    if (!name || name.includes('$') || name.includes('{') || name.includes('}')) continue
    if (!used.has(name)) used.set(name, new Set())
    used.get(name).add(file.replace(/\\/g, '/'))
  }
}

for (const file of files) {
  const source = readFileSync(file, 'utf8')
  // className="a b" / className={'a b'} / className={`a ${x}`}
  for (const match of source.matchAll(/className=(?:"([^"]*)"|'([^']*)')/g)) {
    addAll(match[1] ?? match[2] ?? '', file)
  }
  for (const match of source.matchAll(/className=\{`([^`]*)`\}/g)) {
    addAll((match[1] ?? '').replace(/\$\{[^}]*\}/g, ' '), file)
  }
  for (const match of source.matchAll(/className=\{[^}]*?'([^']*)'[^}]*?\}/g)) {
    addAll(match[1] ?? '', file)
  }
}

const css = readFileSync('src/styles.css', 'utf8')
const missing = []
for (const [name, owners] of [...used].sort()) {
  const pattern = new RegExp(`\\.${name.replace(/[-/\\^$*+?.()|[\]{}]/g, '\\$&')}(?![\\w-])`)
  if (!pattern.test(css)) missing.push([name, [...owners]])
}

if (process.argv.includes('--list')) {
  console.log([...used.keys()].sort().join('\n'))
  process.exit(0)
}

console.log(`TSX 里用到的类名: ${used.size}`)
if (missing.length === 0) {
  console.log('样式表覆盖: 全部命中')
} else {
  console.log(`样式表未提及的类名: ${missing.length}`)
  for (const [name, owners] of missing) console.log(`  ${name}  <- ${owners.join(', ')}`)
}
