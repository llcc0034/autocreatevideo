export const commitLatestRequest = async <T>(
  request: Promise<T>,
  isCurrent: () => boolean,
  commit: (value: T) => void,
  fail: (cause: unknown) => void,
): Promise<void> => {
  try {
    const value = await request
    if (isCurrent()) commit(value)
  } catch (cause) {
    if (isCurrent()) fail(cause)
  }
}
