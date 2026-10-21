// The table twin every chart carries: the accessible, colour-free way to read each value.
export function TableView({ head, rows }: { head: string[]; rows: (string | number)[][] }) {
  return (
    <details className="mt-3 text-sm">
      <summary className="cursor-pointer select-none text-xs text-ink-2 hover:text-ink">
        Table view
      </summary>
      <table className="mt-2 w-full text-left">
        <thead className="text-xs text-muted">
          <tr>
            {head.map((h, i) => (
              <th key={h} className={`py-1 font-medium ${i ? "text-right" : ""}`}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="tabular">
          {rows.map((r, i) => (
            <tr key={i} className="border-t border-hair">
              {r.map((c, j) => (
                <td key={j} className={`py-1 ${j ? "text-right text-ink-2" : "text-ink"}`}>
                  {c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}
