// @vitest-environment jsdom
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import TaskRunner from './TaskRunner.vue'
import TaskDetail from './TaskDetail.vue'
import AgentInspector from './AgentInspector.vue'
import ModelSettings from './ModelSettings.vue'
import { reactive } from 'vue'
import { clone, terminal } from '../api'
import type { Agent, Configuration, Task } from '../types'

describe('operator control surface', () => {
  it('sends routing and confined project information', async () => {
    const w=mount(TaskRunner,{props:{project:{name:'demo',workspace:'/safe'},profiles:['fast'],editable:true,busy:false}})
    await w.get('textarea').setValue('Review the project')
    await w.get('form').trigger('submit')
    expect(w.emitted('run')?.[0]?.[0]).toMatchObject({goal:'Review the project',agent:'auto',project:'demo',workspace:'/safe',model_profile:null})
  })
  it('viewers cannot submit tasks', () => {
    const w=mount(TaskRunner,{props:{project:{name:'demo',workspace:'/safe'},profiles:[],editable:false,busy:false}})
    expect(w.get('button').attributes('disabled')).toBeDefined()
  })
  it('separates unsupported claims from recorded evidence', () => {
    const task:Task={id:'1',goal:'review',status:'completed',created_at:new Date().toISOString(),updated_at:new Date().toISOString(),result:{summary:'All tests passed'},error:null,options:{project:'demo',workspace:'/safe',agent:'coder'}}
    const w=mount(TaskDetail,{props:{task,events:[],evidence:{tool_calls:[],files_modified:[],tests_executed:[],verification_actions:[],scope:'Recorded tool execution only'},editable:true,inspection:false}})
    expect(w.text()).toContain('Unverified claims')
    expect(w.text()).toContain('All tests passed')
    expect(w.text()).toContain('No successful file writes recorded')
    expect(w.text()).toContain('disabled')
  })
  it('only the mandate has an editor', async () => {
    const agent:Agent={id:'coder',name:'coder',role:'coder',profile:'fast',provider:'local',model:'qwen',location:'local',endpoint:'http://localhost',max_tokens:8192,tools:['filesystem.write'],risk_limit:'high',state:'idle',current_task:null,connection:'not_tested',mandate:'Implement changes',security_rules:'No shell',system_prompt:'Actual prompt'}
    const w=mount(AgentInspector,{props:{agent,editable:true}})
    expect(w.findAll('textarea')).toHaveLength(1)
    await w.get('textarea').setValue('Read before editing')
    await w.findAll('button')[1]!.trigger('click')
    expect(w.emitted('save')?.[0]).toEqual(['Read before editing'])
    expect(w.text()).toContain('Read-only')
  })
  it('edits model assignments from reactive backend data', async () => {
    const configuration:Configuration={profiles:{fast:{provider:'scripted',model:'scripted',base_url:'http://localhost:11434/v1',api_key_env:null,max_tokens:1024,temperature:0,timeout_seconds:30,retry_count:1,structured_output:'schema',local_server:'ollama',credential_configured:false}},agents:{coder:{profile:'fast',mandate:null}}}
    const w=mount(ModelSettings,{props:{configuration:reactive(configuration),editable:true}})
    const save=w.findAll('button').find(b=>b.text()==='Save configuration')!
    await save.trigger('click')
    const body=w.emitted('save')?.[0]?.[0] as Configuration
    expect(body.profiles.fast?.model).toBe('scripted')
    expect(body.profiles.fast).not.toHaveProperty('credential_configured')
    expect(clone(reactive(configuration))).toEqual(configuration)
  })
  it('recognizes parked and terminal tasks',()=>{expect(terminal('waiting_for_approval')).toBe(true);expect(terminal('running')).toBe(false)})
})
