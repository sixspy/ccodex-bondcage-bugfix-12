export const stubsViewTemplate = `
<section class="panel">
  <h2>增补 / 拆笼</h2>
  <div class="stub-banner">
    增补入笼仍为空桩：只登记 job 与审计，不改笼额、不生子笼。<br/>
    拆笼分拨已落地，原笼关闭只落一路且当日不翻案：<b>甲·整笔切分</b> = 原笼号标「已拆分」只读、
    可用归零，核验/解笼/罚没/增补写口一律正式拒收；<b>乙·影子子笼</b> = 原笼保持活跃，
    子笼晋升行零改回退，只留查询快照、不入拨付。两路互斥，禁止混用。
  </div>
  <div class="grid-2">
    <div>
      <h3>增补入笼（空桩）</h3>
      <div class="form-row">
        <div>
          <label>笼</label>
          <select v-model.number="topupForm.cage_id">
            <option :value="0">请选择</option>
            <option v-for="c in topupCandidates" :key="'t'+c.id" :value="c.id">{{ c.cage_code }} · {{ c.status }}</option>
          </select>
        </div>
        <div>
          <label>金额</label>
          <input type="number" v-model.number="topupForm.amount" min="1"/>
        </div>
        <div>
          <label>模式</label>
          <select v-model="topupForm.mode">
            <option>同笼扩代</option>
            <option>新笼桥接</option>
          </select>
        </div>
      </div>
      <div class="actions">
        <button @click="doTopupStub">登记增补空桩</button>
      </div>
      <table>
        <thead><tr><th>ID</th><th>笼</th><th>金额</th><th>模式</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="j in topupJobs" :key="j.id">
            <td>{{ j.id }}</td>
            <td>{{ j.cage_code }}</td>
            <td>{{ fmt(j.amount) }}</td>
            <td>{{ j.mode }}</td>
            <td>{{ j.status }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div>
      <h3>拆笼分拨</h3>
      <div class="form-row">
        <div>
          <label>原笼（仅未拆的已冻结笼）</label>
          <select v-model.number="splitForm.cage_id">
            <option :value="0">请选择</option>
            <option v-for="c in splitCandidates" :key="'s'+c.id" :value="c.id">
              {{ c.cage_code }} · 可用 {{ fmt(c.available) }}
            </option>
          </select>
        </div>
        <div>
          <label>左金额</label>
          <input type="number" v-model.number="splitForm.left_amount" min="1"/>
        </div>
        <div>
          <label>右金额</label>
          <input type="number" v-model.number="splitForm.right_amount" min="1"/>
        </div>
        <div>
          <label>路径</label>
          <select v-model="splitForm.mode">
            <option>整笔切分</option>
            <option>影子子笼</option>
          </select>
        </div>
      </div>
      <div class="hint" v-if="!splitCandidates.length">
        没有可拆原笼：已核验、已解笼、已拆分或已落纪要的笼一律禁止再拆。
      </div>
      <div class="actions">
        <button @click="doSplitStub">提交拆笼</button>
      </div>
    </div>
  </div>

  <h3 style="margin-top:16px">原笼关闭纪要（当日锁定，不得翻案）</h3>
  <table>
    <thead>
      <tr><th>原笼</th><th>路径</th><th>模式</th><th>原笼命运</th><th>锁定日</th><th>禁翻案</th></tr>
    </thead>
    <tbody>
      <tr v-for="m in splitMinutes" :key="m.id">
        <td>{{ m.cage_code }}</td>
        <td>{{ m.path }}</td>
        <td>{{ m.mode }}</td>
        <td>
          <span class="badge" :class="m.closed_status==='已拆分'?'fail':'frozen'">{{ m.closed_status }}</span>
        </td>
        <td>{{ m.locked_on }}</td>
        <td>{{ m.no_reopen ? '是' : '否' }}</td>
      </tr>
      <tr v-if="!splitMinutes.length"><td colspan="6" class="hint">尚无拆笼纪要。</td></tr>
    </tbody>
  </table>

  <h3 style="margin-top:16px">切分腿 / 影子快照</h3>
  <table>
    <thead>
      <tr><th>Job</th><th>原笼</th><th>腿</th><th>金额</th><th>模式</th><th>状态</th><th>子笼</th><th>可拨付</th></tr>
    </thead>
    <tbody>
      <tr v-for="l in splitLegs" :key="l.id">
        <td>{{ l.job_id }}</td>
        <td>{{ l.parent_code }}</td>
        <td>{{ l.leg }}</td>
        <td>{{ fmt(l.amount) }}</td>
        <td>{{ l.mode }}</td>
        <td>
          <span class="badge" :class="l.status==='生效'?'pass':'frozen'">{{ l.status }}</span>
        </td>
        <td>{{ l.child_code || '—（无子笼）' }}</td>
        <td>{{ l.status === '生效' ? '是' : '否（快照）' }}</td>
      </tr>
      <tr v-if="!splitLegs.length"><td colspan="8" class="hint">尚无拆笼腿。</td></tr>
    </tbody>
  </table>

  <h3 style="margin-top:16px">拆笼任务</h3>
  <table>
    <thead><tr><th>ID</th><th>笼</th><th>左</th><th>右</th><th>模式</th><th>状态</th></tr></thead>
    <tbody>
      <tr v-for="j in splitJobs" :key="'j'+j.id">
        <td>{{ j.id }}</td>
        <td>{{ j.cage_code }}</td>
        <td>{{ fmt(j.left_amount) }}</td>
        <td>{{ fmt(j.right_amount) }}</td>
        <td>{{ j.mode }}</td>
        <td><span class="badge" :class="j.status==='已切分'?'done':'frozen'">{{ j.status }}</span></td>
      </tr>
    </tbody>
  </table>
</section>
`;
