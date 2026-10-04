import fs from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

const css = fs.readFileSync(path.join(__dirname, '../styles/wtf-theme.css'), 'utf8')
const index = fs.readFileSync(path.join(__dirname, '../index.css'), 'utf8')
const sidebar = fs.readFileSync(path.join(__dirname, '../components/Sidebar/AppSidebar.vue'), 'utf8')

// Token names verified against frappe-ui 1.0.0-beta.29 (tailwind/generated/colors.json):
// --surface-*, --ink-*, --outline-*; radius vars are --radius-{4,6,7,lg,xl}.
describe('WTF theme layer', () => {
	it('is imported last so it wins the cascade', () => {
		const imports = index.match(/@import[^;]+;/g) ?? []
		expect(imports.at(-1)).toContain('wtf-theme.css')
	})

	it('maps brand colours onto real frappe-ui tokens', () => {
		for (const [token, val] of [
			['--surface-gray-7', '#0d0d0d'],
			['--surface-gray-10', '#0d0d0d'],
			['--surface-red-7', '#d2000b'],
			['--surface-red-8', '#b0000a'],
			['--ink-red-6', '#d2000b'],
			['--surface-sidebar', '#0d0d0d'],
			['--outline-gray-1', '#eef0f3'],
		])
			expect(css.toLowerCase()).toMatch(new RegExp(`${token}\\s*:\\s*${val}`))
	})

	it('does not clobber dark mode with light values', () => {
		expect(css).toMatch(/:root:not\(\[data-theme=['"]dark['"]\]\)/)
	})

	it('uses Inter and the real rounded radius vars, no Hinglish', () => {
		expect(css).toMatch(/font-family:[^;]*Inter/)
		for (const t of ['--radius-4: 8px', '--radius-6: 12px', '--radius-7: 16px'])
			expect(css).toContain(t)
		expect(css).not.toMatch(/\b(hai|karo|mein|seekho)\b/i)
	})

	it('lights the ink tokens inside the sidebar scope only', () => {
		expect(sidebar).toContain('wtf-sidebar')
		const block = css.match(/\.wtf-sidebar\s*\{[^}]*\}/)?.[0] ?? ''
		for (const t of ['--ink-gray-5', '--ink-gray-7', '--ink-gray-8', '--ink-gray-9'])
			expect(block).toContain(t)
		const rootBlock = css.match(/:root:not[^{]*\{[^}]*\}/)?.[0] ?? ''
		expect(rootBlock).not.toMatch(/--ink-gray-/)
	})
})
