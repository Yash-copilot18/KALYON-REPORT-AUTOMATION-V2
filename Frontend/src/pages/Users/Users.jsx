import React, { useState } from 'react'
import { PageHeader, SearchInput, AsyncButton, Skeleton } from '../../components/Common'
import { useFetch } from '../../hooks/useFetch'
import { fetchUsers } from '../../services/api'
import { initials, rolePillClass } from '../../utils/helpers'
import { useApp } from '../../utils/AppContext'

const AVATAR_COLORS = [
  'from-blue-500 to-purple-600',
  'from-teal-500 to-blue-600',
  'from-purple-500 to-pink-600',
  'from-amber-500 to-orange-600',
  'from-green-500 to-teal-600',
]

function UserForm({ user = {} }) {
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1">
          <label className="form-label">First Name</label>
          <input className="form-control" defaultValue={user.name?.split(' ')[0] || ''} />
        </div>
        <div className="flex flex-col gap-1">
          <label className="form-label">Last Name</label>
          <input className="form-control" defaultValue={user.name?.split(' ')[1] || ''} />
        </div>
      </div>
      <div className="flex flex-col gap-1">
        <label className="form-label">Email</label>
        <input type="email" className="form-control" defaultValue={user.email || ''} />
      </div>
      <div className="flex flex-col gap-1">
        <label className="form-label">Role</label>
        <select className="form-control" defaultValue={user.role || 'Engineer'}>
          <option>Admin</option><option>Manager</option>
          <option>Engineer</option><option>Analyst</option>
        </select>
      </div>
      <div className="flex flex-col gap-1">
        <label className="form-label">Department</label>
        <select className="form-control" defaultValue={user.dept || ''}>
          <option>Operations</option><option>Maintenance</option>
          <option>Analytics</option><option>Management</option><option>Monitoring</option>
        </select>
      </div>
      {!user.id && (
        <div className="flex flex-col gap-1">
          <label className="form-label">Temporary Password</label>
          <input type="password" className="form-control" placeholder="Auto-generate if blank" />
        </div>
      )}
    </div>
  )
}

export default function Users() {
  const { openModal } = useApp()
  const { data: rawData, loading } = useFetch(fetchUsers)
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState('All')

  // Normalize — fetchUsers returns array directly
  const allUsers = Array.isArray(rawData) ? rawData : []

  const rows = allUsers.filter(u => {
    const matchS = !search || [u.name, u.email, u.role, u.dept].some(
      v => v && v.toLowerCase().includes(search.toLowerCase())
    )
    const matchF = filter === 'All' || u.role === filter
    return matchS && matchF
  })

  return (
    <div>
      <PageHeader title="User Management" subtitle="Manage platform access and roles">
        <button className="btn btn-success btn-sm"
          onClick={() => openModal('Add New User', <UserForm />)}>
          + Add User
        </button>
      </PageHeader>

      {/* Stats */}
      <div className="grid grid-cols-4 gap-3 mb-4">
        {[
          { l:'Total Users', v: allUsers.length,                              c:'blue'  },
          { l:'Active Now',  v: allUsers.filter(u => u.active).length,        c:'green' },
          { l:'Admins',      v: allUsers.filter(u => u.role === 'Admin').length,    c:'red'   },
          { l:'Engineers',   v: allUsers.filter(u => u.role === 'Engineer').length, c:'amber' },
        ].map(({ l, v, c }) => (
          <div key={l} className={`kpi-card kpi-${c}`}>
            <div className="font-mono text-[10px] text-ge-text3 uppercase tracking-widest mb-1">{l}</div>
            <div className="font-mono text-2xl font-semibold text-ge-text1">{v}</div>
          </div>
        ))}
      </div>

      <div className="card">
        <div className="card-title justify-between">
          <span>Platform Users</span>
          <div className="flex items-center gap-2 flex-wrap">
            {['All','Admin','Manager','Engineer','Analyst'].map(f => (
              <button key={f} onClick={() => setFilter(f)}
                className={`btn btn-sm ${filter === f ? 'btn-primary' : 'btn-outline'}`}>
                {f}
              </button>
            ))}
            <SearchInput value={search} onChange={setSearch}
              placeholder="Search users..." className="w-44" />
          </div>
        </div>

        {loading ? (
          <div className="space-y-1">
            {[...Array(5)].map((_, i) => <Skeleton key={i} h="h-12" />)}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>User</th><th>Email</th><th>Role</th>
                  <th>Department</th><th>Last Active</th><th>Status</th><th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {rows.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="text-center text-ge-text3 py-8">
                      No users found
                    </td>
                  </tr>
                ) : rows.map((u, i) => (
                  <tr key={u.id}>
                    <td>
                      <div className="flex items-center gap-2.5">
                        <div className={`w-7 h-7 rounded-full flex items-center justify-center
                                        text-[10px] font-bold text-white bg-gradient-to-br
                                        flex-shrink-0 ${AVATAR_COLORS[i % AVATAR_COLORS.length]}`}>
                          {initials(u.name || 'U')}
                        </div>
                        <span className="font-medium text-ge-text1">{u.name}</span>
                      </div>
                    </td>
                    <td className="text-ge-blue text-[12px]">{u.email}</td>
                    <td><span className={rolePillClass(u.role)}>{u.role}</span></td>
                    <td>{u.dept}</td>
                    <td className="font-mono text-[11px] text-ge-text3">{u.last}</td>
                    <td>
                      <div className="flex items-center gap-1.5">
                        <span className={`w-1.5 h-1.5 rounded-full
                          ${u.active ? 'bg-ge-success' : 'bg-ge-border2'}`} />
                        <span className="text-[11px] text-ge-text3">
                          {u.active ? 'Online' : 'Offline'}
                        </span>
                      </div>
                    </td>
                    <td>
                      <div className="flex gap-1.5">
                        <button className="btn btn-outline btn-sm"
                          onClick={() => openModal(`Edit User — ${u.name}`, <UserForm user={u} />)}>
                          ✎ Edit
                        </button>
                        <AsyncButton className="btn btn-danger btn-sm" successMsg="User removed">
                          ✕
                        </AsyncButton>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}