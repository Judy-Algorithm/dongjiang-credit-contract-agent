import {api, setCsrfToken} from "./api.js?v=20260801-smooth2"

let state = {loaded:false, setupRequired:false, authenticated:false, user:null}

export async function loadAuth({refresh = false} = {}) {
  if (state.loaded && !refresh) return state
  const data = await api.authStatus()
  setCsrfToken(data.csrf_token)
  state = {
    loaded:true,
    setupRequired:Boolean(data.setup_required),
    authenticated:Boolean(data.authenticated),
    user:data.user || null,
  }
  return state
}

export function currentAuth() {
  return state
}

export function setAuthenticated(data) {
  setCsrfToken(data.csrf_token)
  state = {loaded:true, setupRequired:false, authenticated:true, user:data.user}
}

export function clearAuth() {
  setCsrfToken("")
  state = {loaded:true, setupRequired:false, authenticated:false, user:null}
}

export function hasRole(...roles) {
  const current = new Set(state.user?.roles || [])
  return current.has("admin") || roles.some((role) => current.has(role))
}
