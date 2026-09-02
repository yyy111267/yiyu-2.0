/* ============================================================
   Mock 数据层  (window.Yiyu.data)
   原型演示用，所有交互均基于此静态数据 + localStorage 持久化
   ============================================================ */
(function () {
  const Yiyu = (window.Yiyu = window.Yiyu || {});

  /* ---------------- 认知库条目 ---------------- */
  const cognitionSeed = [
    {
      id: "c1", title: "只投自己看得懂的生意",
      content: "优先研究商业模式简单、现金流可预测的公司。看不懂盈利来源、依赖连续融资或叙事驱动的公司，一律不进入核心池。",
      type: "能力圈", tags: ["#通用", "#认知纪律"], source: "manual",
      createdAt: "2026-05-12", updatedAt: "2026-08-01", injected: true
    },
    {
      id: "c2", title: "ROE 长期低于 15% 不碰",
      content: "除非处于战略投入期且有明确拐点，否则连续 5 年 ROE < 15% 的公司，说明资本回报能力弱，直接排除。",
      type: "选股标准", tags: ["#贵州茅台", "#消费", "#财务指标"], source: "manual",
      createdAt: "2026-04-20", updatedAt: "2026-07-18", injected: true
    },
    {
      id: "c3", title: "单一标的仓位上限 25%",
      content: "任何单一标的初始建仓不超过组合的 25%，行业集中度不超过 50%，避免黑天鹅导致不可逆损失。",
      type: "仓位原则", tags: ["#通用", "#风控"], source: "manual",
      createdAt: "2026-03-08", updatedAt: "2026-06-30", injected: true
    },
    {
      id: "c4", title: "基本面恶化即减仓，不止损靠信仰",
      content: "卖出理由是『当初买入的逻辑被证伪』，而非股价涨跌。当核心假设被数据推翻，无论盈亏都应立即重估。",
      type: "卖出纪律", tags: ["#通用", "#交易纪律"], source: "ai",
      createdAt: "2026-06-15", updatedAt: "2026-07-22", injected: true
    },
    {
      id: "c5", title: "白酒要看预收账款与渠道库存",
      content: "研究白酒不能只看营收增速，预收账款（合同负债）是先行指标，渠道库存高企往往预示后续增长失速。",
      type: "行业认知", tags: ["#白酒", "#贵州茅台", "#消费"], source: "ai",
      createdAt: "2026-07-02", updatedAt: "2026-08-05", injected: true
    },
    {
      id: "c6", title: "现金流比利润更可靠",
      content: "经营现金流/净利润长期 < 0.8 的公司，利润质量存疑，需警惕应收账款或存货堆积。",
      type: "选股标准", tags: ["#通用", "#财务指标"], source: "manual",
      createdAt: "2026-05-30", updatedAt: "2026-07-10", injected: false
    },
    {
      id: "c7", title: "创新药只看管线与里程碑",
      content: "未盈利创新药不看 PE，核心看在研管线进度、关键临床终点数据、以及现金可支撑月数（<18 个月危险）。",
      type: "行业认知", tags: ["#创新药", "#医药"], source: "ai",
      createdAt: "2026-06-28", updatedAt: "2026-07-30", injected: false
    },
    {
      id: "c8", title: "不在情绪高点追高买入",
      content: "当某标的成为全民共识、换手率与估值同时处于历史极高分位时，即使基本面好也暂缓加仓。",
      type: "交易纪律", tags: ["#通用", "#交易纪律"], source: "manual",
      createdAt: "2026-04-02", updatedAt: "2026-06-12", injected: true
    }
  ];

  /* ---------------- 历史对话 ---------------- */
  const history = [
    { id: "h1", title: "研究贵州茅台的护城河", group: "今天", time: "14:20" },
    { id: "h2", title: "宁德时代还能撑多久", group: "今天", time: "10:05" },
    { id: "h3", title: "片仔癀估值贵不贵", group: "昨天", time: "21:30" },
    { id: "h4", title: "招商银行资产质量分析", group: "昨天", time: "16:11" },
    { id: "h5", title: "英伟达商业模式拆解", group: "更早", time: "08-12" },
    { id: "h6", title: "比亚迪护城河是否稳固", group: "更早", time: "08-09" }
  ];

  /* ---------------- 公司研究样本（贵州茅台） ---------------- */
  const researchSample = {
    "贵州茅台": {
      overview: {
        name: "贵州茅台", code: "600519.SH", rating: "A",
        profile: {
          "行业": "白酒 · 高端消费品", "商业模式": "品牌溢价 + 稀缺产能",
          "生命周期": "成熟", "盈利状态": "高利厚利", "资产轻重": "轻资产",
          "收入类型": "产品销售（预收款驱动）", "监管": "食品饮料"
        },
        conclusion: "茅台具备强品牌护城河与极致现金流能力，当前估值处于历史中高位，安全边际依赖长期持有与分红再投。",
        confidence: 0.82,
        injectedCog: ["c2", "c5"]
      },
      thinking: [
        { t: "意图识别与实体锁定", d: "解析『茅台护城河』→ 锁定 600519.SH，研究焦点：护城河稳固性", done: true },
        { t: "可研究性评级", d: "财务披露充分、上市超 20 年 → 评级 A", done: true },
        { t: "策略路由", d: "命中 G1 成熟价值型主包 + 消费补充包", done: true },
        { t: "多角色并行取证", d: "商业模式/财务/竞争/风险四角色同步采集证据", done: true },
        { t: "证据质检与计算校验", d: "ROE、毛利率双源验证，单位统一（元/亿元）", done: true },
        { t: "跨维度信号一致性", d: "预收账款与营收增长一致，无异常触发", done: true },
        { t: "论点与估值形成", d: "生成论点树 + 三情景估值区间", done: true },
        { t: "准出抽检", d: "关键数字均有来源，通过", done: true }
      ],
      evidence: [
        { claim: "品牌护城河极深，定价权强", type: "fact", ev: "毛利率连续 10 年 > 90%，行业第一", counter: "—", conf: 0.95, src: "年报 / 行业对比" },
        { claim: "经营现金流充沛且质量高", type: "fact", ev: "经营现金流/净利润 5 年均值为 1.05", counter: "—", conf: 0.9, src: "Wind 计算" },
        { claim: "增长更多来自提价而非放量", type: "judgment", ev: "基酒产能天花板约 5.6 万吨，量增有限", counter: "直营化可提升吨价", conf: 0.7, src: "产能公告" },
        { claim: "估值处于历史中高位", type: "estimate", ev: "当前 PE-TTM 约 28x，近 5 年 60% 分位", counter: "若降息则估值有支撑", conf: 0.65, src: "行情快照" }
      ],
      thesis: [
        { kind: "core", t: "核心论点：茅台是 A 股稀缺的『品牌+稀缺』双护城河资产", d: "适合长期持有，短期估值需耐心" },
        { kind: "support", t: "支持：毛利率 >90%、ROE 长期 >30%", d: "资本回报能力行业顶尖" },
        { kind: "support", t: "支持：预收账款（合同负债）持续高位", d: "需求旺盛、渠道话语权强" },
        { kind: "against", t: "反证：量增受产能天花板约束", d: "增长依赖提价与直营化，天花板可见" },
        { kind: "key", t: "关键假设：高端消费需求不出现结构性坍塌", d: "跟踪指标：批价、预收款、直营占比" }
      ],
      valuation: {
        metrics: [
          { k: "PE-TTM", v: "28.4x" }, { k: "ROE(ttm)", v: "34.2%" },
          { k: "毛利率", v: "91.8%" }, { k: "经营现金流/净利", v: "1.05" },
          { k: "股息率", v: "3.1%" }
        ],
        scenarios: [
          { name: "悲观", range: "1,380 – 1,520", cls: "pessimistic" },
          { name: "中性", range: "1,650 – 1,850", cls: "neutral" },
          { name: "乐观", range: "2,000 – 2,300", cls: "optimistic" }
        ]
      },
      deposit: {
        title: "茅台护城河 = 品牌溢价 + 稀缺产能",
        content: "高端白酒中唯一具备绝对定价权与产能硬约束的标的，长期持有逻辑依赖批价稳定与预收款先行指标。"
      }
    }
  };

  /* ---------------- 用户 ---------------- */
  const user = { name: "投研同学", email: "user@yiyu.ai" };

  Yiyu.data = { cognitionSeed, history, researchSample, user };
})();
