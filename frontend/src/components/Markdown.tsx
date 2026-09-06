import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/**
 * Document-style rendering for assistant answers.
 *
 * These answers are analytical: they routinely contain tables of figures,
 * nested caveats and long prose. The element overrides below are tuned for
 * reading that, not for chat one-liners -- notably numeric table cells are
 * tabular-nums so columns of currency line up, and every table gets its own
 * horizontal scroll container so a wide result never widens the page.
 */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="text-[15px] leading-[1.7] text-slate-700">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          p: (p) => <p className="mb-3 last:mb-0" {...p} />,
          h1: (p) => <h1 className="mt-6 mb-2 text-[17px] font-bold tracking-tight text-slate-900 first:mt-0" {...p} />,
          h2: (p) => <h2 className="mt-6 mb-2 text-[16px] font-bold tracking-tight text-slate-900 first:mt-0" {...p} />,
          h3: (p) => <h3 className="mt-5 mb-1.5 text-[15px] font-semibold text-slate-900 first:mt-0" {...p} />,
          ul: (p) => <ul className="mb-3 list-disc space-y-1 pl-5 marker:text-slate-400" {...p} />,
          ol: (p) => <ol className="mb-3 list-decimal space-y-1 pl-5 marker:text-slate-400" {...p} />,
          li: (p) => <li className="pl-0.5" {...p} />,
          strong: (p) => <strong className="font-semibold text-slate-900" {...p} />,
          a: (p) => (
            <a className="font-medium text-brand-600 underline underline-offset-2 hover:text-brand-700"
               target="_blank" rel="noreferrer noopener" {...p} />
          ),
          blockquote: (p) => (
            <blockquote className="mb-3 border-l-2 border-slate-200 pl-3 text-slate-600" {...p} />
          ),
          hr: () => <hr className="my-5 border-slate-200" />,
          table: (p) => (
            <div className="mb-4 max-w-full overflow-x-auto rounded-lg border border-slate-200 scroll-thin">
              <table className="w-full border-collapse text-[13.5px]" {...p} />
            </div>
          ),
          thead: (p) => <thead className="bg-slate-50" {...p} />,
          th: (p) => (
            <th className="whitespace-nowrap border-b border-slate-200 px-3 py-2 text-left text-[11px]
                           font-semibold uppercase tracking-wide text-slate-500" {...p} />
          ),
          td: (p) => (
            <td className="border-b border-slate-100 px-3 py-2 align-top tabular-nums
                           last:border-r-0 [tr:last-child_&]:border-b-0" {...p} />
          ),
          code: ({ className, children, ...rest }) => {
            const isBlock = /language-/.test(className || '')
            if (isBlock) {
              return (
                <pre className="mb-3 overflow-x-auto rounded-lg border border-slate-200 bg-slate-50 p-3 scroll-thin">
                  <code className="font-mono text-[12.5px] text-slate-800" {...rest}>{children}</code>
                </pre>
              )
            }
            return (
              <code className="rounded border border-slate-200 bg-slate-50 px-1 py-0.5
                               font-mono text-[12.5px] text-slate-800" {...rest}>{children}</code>
            )
          },
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
}
