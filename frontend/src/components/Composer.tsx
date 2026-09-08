import { ArrowUp, Sparkles } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

/**
 * Pill composer. Enter sends, Shift+Enter inserts a newline, and the textarea
 * grows with the content up to a cap before scrolling — so a long multi-line
 * question stays editable without the composer eating the transcript.
 *
 * The reference also shows attach and insert-chart buttons; neither exists in
 * this backend, so neither is drawn.
 */
export function Composer({
  onSend, disabled, placeholder = 'Ask about the business...',
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
    el.style.height = `${Math.min(el.scrollHeight, 168)}px`
  }, [value])

  const submit = () => {
    const text = value.trim()
    if (!text || disabled) return
    setValue('')
    onSend(text)
  }

  const multiline = value.includes('\n') || value.length > 90

  return (
    <div className="mx-auto w-full max-w-[880px]">
      <div className={`flex items-end gap-2 border border-line bg-white py-2 pl-4 pr-2
                       shadow-lift transition-all duration-150
                       focus-within:border-ink-400/40 focus-within:shadow-float
                       ${multiline ? 'rounded-3xl' : 'rounded-full'}`}>
        <Sparkles size={17} className="mb-2 shrink-0 text-brand-500" aria-hidden />
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
          className="max-h-[168px] flex-1 resize-none bg-transparent py-1.5 text-[14.5px]
                     leading-6 text-ink-900 placeholder:text-ink-400 focus:outline-none
                     disabled:opacity-60 scroll-thin"
        />
        <button
          type="button"
          onClick={submit}
          disabled={disabled || !value.trim()}
          aria-label="Send question"
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full transition-all
                     enabled:bg-brand-500 enabled:text-white
                     enabled:shadow-[0_1px_3px_rgba(214,32,47,.35)]
                     enabled:hover:bg-brand-600 enabled:active:scale-95
                     disabled:cursor-not-allowed disabled:bg-app disabled:text-ink-400"
        >
          <ArrowUp size={17} strokeWidth={2.6} />
        </button>
      </div>
    </div>
  )
}
