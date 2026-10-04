/**
 * Mobile layout + brand-name regressions found in the live walkthrough.
 *
 * 1. CourseOverview's left column clipped text at 390px: in a column flex with
 *    `items-start` a child sizes to its content, so it needs `w-full`.
 * 2. Learner-facing copy said "Frappe Learning" instead of the site brand.
 */
import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { parse } from '@vue/compiler-sfc'

const SRC = resolve(__dirname, '..')

const walk = (dir: string): string[] =>
	readdirSync(dir).flatMap((name) => {
		const p = join(dir, name)
		if (name === 'tests' || name === 'node_modules') return []
		return statSync(p).isDirectory() ? walk(p) : [p]
	})

const staticClass = (node: any): string => {
	const prop = (node.props || []).find(
		(p: any) => p.type === 6 && p.name === 'class'
	)
	return prop?.value?.content || ''
}

// Elements with a responsive width but no base `w-full` / `w-*`.
const needsFullWidth = (cls: string) =>
	/(^|\s)(sm|md|lg):w-/.test(cls) && !/(^|\s)w-/.test(cls)

const collectOffenders = (file: string): string[] => {
	const { descriptor } = parse(readFileSync(file, 'utf8'))
	const ast: any = descriptor.template?.ast
	const out: string[] = []
	const visit = (node: any) => {
		const cls = staticClass(node)
		const isColStart = /(^|\s)flex-col(\s|$)/.test(cls) && /(^|\s)items-start(\s|$)/.test(cls)
		for (const child of node.children || []) {
			if (child.type !== 1) continue
			if (isColStart && needsFullWidth(staticClass(child)))
				out.push(`${file.replace(SRC, 'src')}: <${child.tag} class="${staticClass(child)}">`)
			visit(child)
		}
	}
	if (ast) visit(ast)
	return out
}

describe('flex-col items-start parents', () => {
	it('never hold a responsive-width child without a base width', () => {
		const offenders = walk(SRC)
			.filter((f) => f.endsWith('.vue'))
			.flatMap(collectOffenders)
		expect(offenders).toEqual([])
	})

	it('CourseOverview and BatchOverview left columns are full width on mobile', () => {
		for (const f of [
			'pages/Courses/CourseOverview.vue',
			'pages/Batches/BatchOverview.vue',
		]) {
			expect(readFileSync(join(SRC, f), 'utf8')).toMatch(/class="w-full md:w-2\/3 /)
		}
	})
})

describe('learner-facing brand copy', () => {
	const read = (f: string) => readFileSync(join(SRC, f), 'utf8')

	it('has no "Frappe Learning" in the persona or email-template copy', () => {
		for (const f of [
			'pages/Forms/PersonaForm.vue',
			'pages/Forms/EmailTemplateForm.vue',
			'components/Settings/EmailTemplate/EmailTemplateAdd.vue',
			'components/Settings/EmailTemplate/EmailTemplateEdit.vue',
			'components/InstallPrompt.vue',
		]) {
			expect(read(f), f).not.toContain('Frappe Learning')
		}
	})

	it('InstallPrompt shows the branding name', async () => {
		vi.resetModules()
		vi.doMock('frappe-ui', () => ({
			Button: { template: '<button><slot /></button>' },
			Popover: { template: '<div><slot /></div>' },
			Dialog: {
				props: ['open'],
				template:
					'<div v-if="open" class="dlg"><slot name="title" /><slot /><slot name="actions" /></div>',
			},
		}))
		vi.doMock('@/stores/session', () => ({
			sessionStore: () => ({ brand: { name: 'WTF Academy' } }),
		}))
		const InstallPrompt = (await import('@/components/InstallPrompt.vue')).default
		const wrapper = mount(InstallPrompt, { global: { mocks: { __: (globalThis as any).__ } } })
		window.dispatchEvent(new Event('beforeinstallprompt'))
		await wrapper.vm.$nextTick()
		expect(wrapper.text()).toContain('Install WTF Academy')
		expect(wrapper.text()).not.toContain('Frappe')
	})
})
