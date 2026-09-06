import { ArrowUp } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

/**
 * Enter sends, Shift+Enter inserts a newline, and the textarea grows with the
 * content up to a cap before scrolling -- so a long multi-line question stays
 * editable without the composer eating the transcript.
 */
export function Composer({
  onSend, disabled, placeholder = 'Ask about the business…',
}: {
  onSend: (text: string) => void
  disabled?: boolean
  placeholder?: string
}) {
  const [value, setValue] = useState('')
  const ref = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = '0px'
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`
  }, [value])

  const submit = () => {
    const text = value.trim()
    if (!text || disabled) return
    setValue('')
    onSend(text)
  }

  return (
    <div>
      <div className="flex items-end gap-2 rounded-2xl border border-slate-200 bg-white p-2
                      shadow-[0_4px_16px_-4px_rgba(15,23,42,.08)] transition-colors
                      focus-within:border-slate-300">
        <textarea
          ref={ref}
          rows={1}
          value={value}
          disabled={disabled}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() }
          }}
          placeholder={placeholder}
          className="max-h-[200px] flex-1 resize-none bg-transparent px-2 py-2 text-[15px]
                     leading-6 text-slate-800 placeholder:text-slate-400 focus:outline-none
                     disabled:opacity-60 scroll-thin"
        />
        <button
          type="button"
          onClick={submit}
          disabled={disabled || !value.trim()}
          aria-label="Send question"
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl transition-all
                     enabled:bg-brand-500 enabled:text-white enabled:shadow-sm
                     enabled:hover:bg-brand-700 enabled:active:scale-95
                     disabled:cursor-not-allowed disabled:bg-slate-100 disabled:text-slate-300"
        >
          <ArrowUp size={16} strokeWidth={2.5} />
        </button>
      </div>
      <p className="mt-2 text-center text-[11px] text-slate-400">
        Answers are generated from live data · every question is logged
      </p>
    </div>
  )
}
