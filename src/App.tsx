import { useCallback, useEffect, useState } from 'react'
import { api } from './api/client'
import { ConfirmDialog } from './components/ConfirmDialog'
import { CreateProjectModal } from './components/CreateProjectModal'
import { SystemDiagnosticsModal } from './components/SystemDiagnosticsModal'
import { TextProviderModal } from './components/TextProviderModal'
import { HomePage } from './pages/HomePage'
import { WorkspacePage } from './pages/WorkspacePage'
import type { CreateProjectInput, Project } from './types'

export default function App() {
  const [projects, setProjects] = useState<Project[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<Project | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [showDiagnostics, setShowDiagnostics] = useState(false)
  const [actionBusy, setActionBusy] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [pendingDelete, setPendingDelete] = useState<Project | null>(null)

  const loadProjects = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setProjects(await api.listProjects())
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '项目读取失败。')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadProjects()
  }, [loadProjects])

  const createProject = async (input: CreateProjectInput) => {
    setCreating(true)
    setCreateError(null)
    try {
      const project = await api.createProject(input)
      setProjects((items) => [project, ...items.filter((item) => item.id !== project.id)])
      setShowCreate(false)
      setSelected(project)
    } catch (reason) {
      setCreateError(reason instanceof Error ? reason.message : '创建项目失败，请稍后重试。')
    } finally {
      setCreating(false)
    }
  }

  const performProjectAction = async (
    project: Project,
    action: 'duplicate' | 'archive' | 'restore' | 'delete',
  ) => {
    setPendingDelete(null)
    setActionBusy(project.id)
    setError(null)
    try {
      if (action === 'duplicate') await api.duplicateProject(project.id)
      else if (action === 'archive') await api.archiveProject(project.id)
      else if (action === 'restore') await api.restoreProject(project.id)
      else await api.deleteProject(project.id)
      await loadProjects()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '项目操作失败。')
    } finally {
      setActionBusy(null)
    }
  }

  const projectAction = async (
    project: Project,
    action: 'duplicate' | 'archive' | 'restore' | 'delete',
  ) => {
    // 删除需要二次确认：确认后由 ConfirmDialog 调用 performProjectAction 真正执行。
    if (action === 'delete') {
      setPendingDelete(project)
      return
    }
    await performProjectAction(project, action)
  }

  if (selected) {
    return <WorkspacePage project={selected} onBack={() => setSelected(null)} />
  }

  return (
    <>
      <HomePage
        projects={projects}
        loading={loading}
        error={error}
        onRetry={() => void loadProjects()}
        onCreate={() => {
          setCreateError(null)
          setShowCreate(true)
        }}
        onOpen={setSelected}
        onSettings={() => setShowSettings(true)}
        onProjectAction={(project, action) => void projectAction(project, action)}
        actionBusy={actionBusy}
      />
      {showCreate && (
        <CreateProjectModal
          onClose={() => setShowCreate(false)}
          onCreate={createProject}
          creating={creating}
          error={createError}
        />
      )}
      {showSettings && (
        <TextProviderModal
          onClose={() => setShowSettings(false)}
          onDiagnostics={() => {
            setShowSettings(false)
            setShowDiagnostics(true)
          }}
        />
      )}
      {showDiagnostics && (
        <SystemDiagnosticsModal onClose={() => setShowDiagnostics(false)} />
      )}
      <ConfirmDialog
        open={Boolean(pendingDelete)}
        title="删除这个项目？"
        description={pendingDelete
          ? `“${pendingDelete.name}”将被删除，项目下的分镜、素材与导出产物都会从项目列表中移除，此操作不可撤销。`
          : ''}
        confirmLabel="确认删除项目"
        tone="danger"
        busy={Boolean(pendingDelete && actionBusy === pendingDelete.id)}
        onConfirm={() => { if (pendingDelete) void performProjectAction(pendingDelete, 'delete') }}
        onCancel={() => setPendingDelete(null)}
      />
    </>
  )
}
