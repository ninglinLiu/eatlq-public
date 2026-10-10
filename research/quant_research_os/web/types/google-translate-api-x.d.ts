declare module "google-translate-api-x" {
  export interface TranslateResult {
    text: string;
    from?: { language: { iso: string } };
  }
  export default function translate(
    text: string,
    options?: { from?: string; to?: string }
  ): Promise<TranslateResult>;
}
