import { ArrowUp, FileText, Image as ImageIcon, Paperclip, Sparkles, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

const ACCEPT = 'image/png,image/jpeg,image/gif,image/webp,application/pdf,.txt,.csv,.tsv,.md,.json,.log'
const MAX_FILES = 5
const MAX_BYTES = 10_000_000

/**
 * Pill composer. Enter sends, Shift+Enter inserts a newline, and the textarea
 * grows with the content up to a cap before scrolling — so a long multi-line
 * question stays editable without the composer eating the transcript.
 *
 * The paperclip attaches pictures and documents (images, PDF, text/CSV) that
 * are sent with this question only. Files can also be pasted or dropped.
 */
export function Composer({
  onSend, disabled, placeholder = 'Ask about the business...',
}: {
  onSend: (text: string, files: File[]) => void
  disabled?: boolean
  placeholder?: string
}) {
  const [value, setValue] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [note, setNote] = useState<string | null>(null)
  const ref = useRef<HTMLTextAreaElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = '0px'
    el.style.height = `${Math.min(el.scrollHeight, 168)}px`
  }, [value])

  const addFiles = (list: FileList | File[] | null) => {
    if (!list) return
    const incoming = Array.from(list)
    const tooBig = incoming.filter((f) => f.size > MAX_BYTES)
    const ok = incoming.filter((f) => f.size <= MAX_BYTES)
    setFiles((prev) => {
      const next = [...prev, ...ok]
      if (next.length > MAX_FILES) setNote(`You can attach up to ${MAX_FILES} files.`)
      else if (tooBig.length) setNote(`${tooBig[0].name} is larger than 10 MB.`)
      else setNote(null)
      return next.slice(0, MAX_FILES)
    })
  }

  const submit = () => {
    const text = value.trim()
    if (!text || disabled) return
    setValue(''); setFiles([]); setNote(null)
    onSend(text, files)
  }

  const multiline = value.includes('\n') || value.length > 90 || files.length > 0

  return (
    <div className="mx-auto w-full max-w-[880px]">
      <div
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => { e.preventDefault(); addFiles(e.dataTransfer.files) }}
        className={`border border-line bg-white py-2 pl-4 pr-2
                    shadow-lift transition-all duration-150
                    focus-within:border-ink-400/40 focus-within:shadow-float
                    ${multiline ? 'rounded-3xl' : 'rounded-full'}`}>
        {files.length > 0 && (
          <div className="flex flex-wrap gap-1.5 pb-2 pt-1">
            {files.map((f, i) => (
              <span key={`${f.name}-${i}`}
                className="flex max-w-[220px] items-center gap-1.5 rounded-lg border border-line
                           bg-app px-2 py-1 text-[12px] text-ink-700">
                {f.type.startsWith('image/')
                  ? <ImageIcon size={13} className="shrink-0 text-brand-500" />
                  : <FileText size={13} className="shrink-0 text-brand-500" />}
                <span className="truncate">{f.name}</span>
                <button type="button" aria-label={`Remove ${f.name}`}
                  onClick={() => setFiles((p) => p.filter((_, j) => j !== i))}
                  className="shrink-0 text-ink-400 hover:text-ink-900">
                  <X size={12} />
                </button>
              </span>
            ))}
          </div>
        )}
        <div className="flex items-end gap-2">
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
            onPaste={(e) => {
              if (e.clipboardData.files.length) { e.preventDefault(); addFiles(e.clipboardData.files) }
            }}
            placeholder={placeholder}
            className="max-h-[168px] flex-1 resize-none bg-transparent py-1.5 text-[14.5px]
                       leading-6 text-ink-900 placeholder:text-ink-400 focus:outline-none
                       disabled:opacity-60 scroll-thin"
          />
          <input
            ref={fileRef} type="file" multiple accept={ACCEPT} hidden
            onChange={(e) => { addFiles(e.target.files); e.target.value = '' }}
          />
          <button
            type="button"
            onClick={() => fileRef.current?.click()}
            disabled={disabled || files.length >= MAX_FILES}
            aria-label="Attach document or picture"
            title="Attach document or picture"
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full text-ink-400
                       transition-colors enabled:hover:bg-app enabled:hover:text-ink-900
                       disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Paperclip size={17} />
          </button>
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
      {note && <p className="mt-1.5 text-center text-[12px] text-amber-700">{note}</p>}
    </div>
  )
}
