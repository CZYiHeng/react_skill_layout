// 方案确认卡片：模型在 ACT 阶段自证「为什么这么做」的留痕。
// 默认折叠——它是决策依据，不是必须逐字读的产物；需要评审时再展开。
export default function SolutionCard({ text }) {
  if (!text) return null
  return (
    <details className="solution">
      <summary className="solution-summary">
        <span className="solution-badge">方案确认</span>
        <span className="solution-hint">为什么这么做 · 点击展开依据</span>
      </summary>
      <div className="solution-body">{text}</div>
    </details>
  )
}
