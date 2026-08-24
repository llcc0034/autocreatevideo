import type {
  NodeMetadataCatalog,
  NodeMetadataResponse,
} from './platformTypes'

const SHA256 = /^[0-9a-f]{64}$/

const invalidCatalog = (reason: string): Error => new Error(`node-metadata 目录不完整：${reason}`)

export const loadCompleteNodeMetadata = async (
  loadPage: (offset: number, limit: number) => Promise<NodeMetadataResponse>,
  pageSize = 200,
): Promise<NodeMetadataCatalog> => {
  if (!Number.isInteger(pageSize) || pageSize < 1 || pageSize > 200) {
    throw invalidCatalog('分页大小必须为 1–200')
  }

  const items: NodeMetadataCatalog['items'] = []
  const classTypes = new Set<string>()
  const visitedOffsets = new Set<number>()
  let offset = 0
  let expectedTotal: number | null = null
  let expectedCatalogSha256 = ''

  while (true) {
    if (visitedOffsets.has(offset)) throw invalidCatalog(`offset ${offset} 重复`)
    visitedOffsets.add(offset)
    const page = await loadPage(offset, pageSize)

    if (page.schemaVersion !== 2) throw invalidCatalog(`不支持 schema v${page.schemaVersion}`)
    if (page.offset !== offset) throw invalidCatalog(`请求 offset ${offset}，响应为 ${page.offset}`)
    if (!Number.isInteger(page.limit) || page.limit < 1 || page.limit > 200) throw invalidCatalog('响应 limit 无效')
    if (!Number.isInteger(page.totalCount) || page.totalCount < 0) throw invalidCatalog('total_count 无效')
    if (!SHA256.test(page.catalogSha256)) throw invalidCatalog('catalog_sha256 无效')
    if (page.items.length > page.limit) throw invalidCatalog('单页条目数超过 limit')

    if (expectedTotal === null) {
      expectedTotal = page.totalCount
      expectedCatalogSha256 = page.catalogSha256
    } else if (page.totalCount !== expectedTotal || page.catalogSha256 !== expectedCatalogSha256) {
      throw invalidCatalog('分页期间目录 total/hash 发生漂移，请重新读取')
    }

    for (const item of page.items) {
      if (!item.classType) throw invalidCatalog('节点 class_type 为空')
      if (classTypes.has(item.classType)) throw invalidCatalog(`节点 ${item.classType} 重复`)
      classTypes.add(item.classType)
      items.push(item)
    }
    if (items.length > page.totalCount) throw invalidCatalog('累计条目数超过 total_count')

    if (page.nextOffset === null) {
      if (page.offset + page.items.length !== page.totalCount || items.length !== page.totalCount) {
        throw invalidCatalog(`终页仅加载 ${items.length}/${page.totalCount} 条`)
      }
      return {
        schemaVersion: 2,
        totalCount: page.totalCount,
        catalogSha256: page.catalogSha256,
        items,
      }
    }

    const expectedNext = page.offset + page.items.length
    if (!page.items.length || page.nextOffset !== expectedNext || page.nextOffset <= page.offset) {
      throw invalidCatalog('next_offset 未严格前进')
    }
    if (page.nextOffset > page.totalCount) throw invalidCatalog('next_offset 超过 total_count')
    offset = page.nextOffset
  }
}
