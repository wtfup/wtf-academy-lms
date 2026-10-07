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

	const sidebarBlock = () => css.match(/\.wtf-sidebar\s*\{[^}]*\}/)?.[0] ?? ''
	const lum = (hex: string) => {
		const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4))
		return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
	}
	const contrast = (a: string, b: string) => {
		const [x, y] = [lum(a), lum(b)].sort((m, n) => n - m)
		return (x + 0.05) / (y + 0.05)
	}
	const tok = (name: string) => sidebarBlock().match(new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})`))?.[1] as string

	it('remaps every bg-surface-* used inside the sidebar to a dark value', () => {
		const used = new Set([...sidebar.matchAll(/bg-surface-([a-z0-9-]+)/g)].map((m) => m[1]))
		for (const u of used) {
			if (u === 'sidebar') continue
			const t = `--surface-${u}`
			expect(sidebarBlock(), t).toContain(t)
			expect(lum(tok(t)), t).toBeLessThan(0.2)
		}
	})

	it('keeps every inverted ink readable (>= 4.5:1) on each sidebar surface', () => {
		const inks = ['--ink-gray-5', '--ink-gray-7', '--ink-gray-8', '--ink-gray-9']
		for (const surf of ['--surface-base', '--surface-elevation-2', '--surface-gray-2'])
			for (const ink of inks) expect(contrast(tok(ink), tok(surf)), `${ink} on ${surf}`).toBeGreaterThanOrEqual(4.5)
		expect(contrast(tok('--ink-gray-8'), tok('--surface-elevation-3'))).toBeGreaterThanOrEqual(4.5)
	})

	it('makes solid buttons brand red inside the sidebar, visible on its cards', () => {
		expect(sidebarBlock().toLowerCase()).toMatch(/--surface-gray-10:\s*#d2000b/)
		expect(sidebarBlock().toLowerCase()).toMatch(/--surface-gray-9:\s*#b0000a/)
		expect(contrast(tok('--ink-base'), tok('--surface-gray-10'))).toBeGreaterThanOrEqual(4.5)
		expect(contrast(tok('--ink-base'), tok('--surface-gray-9'))).toBeGreaterThanOrEqual(4.5)
		expect(contrast(tok('--surface-gray-10'), tok('--surface-base'))).toBeGreaterThanOrEqual(2.5)
	})

	describe('Operator palette and display font', () => {
		it('defines paper, ink and red tokens', () => {
			expect(css).toMatch(/--wtf-paper:\s*#F6F3EE/i)
			expect(css).toMatch(/--wtf-ink:\s*#0B0B0C/i)
			expect(css).toMatch(/--wtf-red:\s*#D2000B/i)
			expect(css).toMatch(/--wtf-red-pressed:\s*#B0000A/i)
			expect(css).toMatch(/--wtf-muted:\s*#6B6862/i)
		})

		it('self-hosts Anton with no external font call', () => {
			expect(css).toMatch(/@font-face\s*\{[^}]*font-family:\s*['"]Anton['"]/)
			expect(css).toMatch(/url\(['"]?\.\.\/assets\/fonts\/anton-latin\.woff2/)
			expect(fs.existsSync(path.join(__dirname, '../assets/fonts/anton-latin.woff2'))).toBe(true)
			expect(css).not.toMatch(/https?:\/\//)
		})

		it('uses Anton for page-level headings only, via .wtf-display', () => {
			expect(css).toMatch(/\.wtf-display\s*\{[^}]*font-family:[^;]*Anton/)
			expect(css).not.toMatch(/\b(h1|h2|\.prose|\.ProseMirror)\s*\{[^}]*Anton/)
		})

		it('paints paper on the page and wires learner headings', () => {
			expect(css).toMatch(/\.wtf-paper\s*\{[^}]*background(-color)?:\s*var\(--wtf-paper\)/)
			const read = (f: string) => fs.readFileSync(path.join(__dirname, '../pages', f), 'utf8')
			expect(read('Courses/CourseOverview.vue')).toContain('wtf-display')
			expect(read('Lesson.vue')).toContain('wtf-display')
		})
	})
})
