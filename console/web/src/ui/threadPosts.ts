import type { ConsoleApi, Post } from "../api/types"

const MAX_PAGES = 20

/** All posts of a thread, page after page (GET /v1/threads/<id>), at most
 * MAX_PAGES pages. Read only. */
export async function loadThread(api: ConsoleApi, threadId: string): Promise<Post[]> {
  const posts: Post[] = []
  let cursor: string | null = null
  for (let i = 0; i < MAX_PAGES; i++) {
    const page = await api.thread(threadId, cursor)
    posts.push(...page.posts)
    if (!page.next) break
    cursor = page.next
  }
  return posts
}
