import { useEffect, useState } from 'react'
import { api, type LlmHealth } from './api/client'
import './App.css'

function App() {
  const [apiOk, setApiOk] = useState<boolean | null>(null)
  const [llm, setLlm] = useState<LlmHealth | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .health()
      .then((h) => setApiOk(h.status === 'ok'))
      .catch((e) => {
        setApiOk(false)
        setError(String(e))
      })
    api
      .llmHealth()
      .then(setLlm)
      .catch((e) => setError(String(e)))
  }, [])

  return (
    <main style={{ maxWidth: 720, margin: '2rem auto', fontFamily: 'system-ui' }}>
      <h1>LLM Market Scoring</h1>
      <p>Local dashboard — Phase 0 scaffold.</p>

      <section style={{ marginTop: '1.5rem' }}>
        <h2>Backend API</h2>
        <p>
          Status:{' '}
          {apiOk === null ? 'checking…' : apiOk ? '🟢 connected' : '🔴 unreachable'}
        </p>
      </section>

      <section style={{ marginTop: '1rem' }}>
        <h2>Local LLM (Ollama / llama-server)</h2>
        {llm ? (
          <div>
            <p>
              {llm.ok ? '🟢' : '🔴'} provider=<code>{llm.provider}</code> · model=
              <code>{llm.default_model}</code>
            </p>
            {llm.available_models && (
              <ul>
                {llm.available_models.map((m) => (
                  <li key={m}>
                    <code>{m}</code>
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : (
          <p>checking…</p>
        )}
      </section>

      {error && <p style={{ color: 'crimson', marginTop: '1rem' }}>Error: {error}</p>}
    </main>
  )
}

export default App

