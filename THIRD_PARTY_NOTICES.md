# 外部ソフトウェア・サービス

配布ZIPには本アプリのコードとオリジナルのデモ音源を収録します。Python、7zr、VOICEVOX、Ollama、LLMの実行ファイルや重みは収録せず、`setup.cmd` が公式配布元から取得します。取得したソフトウェア内のライセンス・著作権表示は保持してください。本書は各権利元の利用条件を置き換えるものではありません。

| 対象 | 利用条件・公式資料 |
|---|---|
| Python embeddable | Python Software Foundation License等。[ライセンス](https://docs.python.org/3/license.html)、[Windows embeddable](https://docs.python.org/3/using/windows.html#the-embeddable-package) |
| 7zr / 7-Zip | GNU LGPL等。展開用の公式コンソール版を取得します。[ライセンス](https://www.7-zip.org/license.txt)、[公式配布](https://www.7-zip.org/download.html) |
| VOICEVOX ENGINE | ソースはLGPL v3／別ライセンス。[ENGINE](https://github.com/VOICEVOX/voicevox_engine)、[LICENSE](https://github.com/VOICEVOX/voicevox_engine/blob/master/LICENSE) |
| VOICEVOX CORE | ソースはMIT。[CORE LICENSE](https://github.com/VOICEVOX/voicevox_core/blob/main/LICENSE) |
| VOICEVOX音声モデル・生成音声 | それぞれの条件に従います。VVMのアプリ組込み・再配布条件と、生成音声の話者別条件は別です。[VVM規約](https://github.com/VOICEVOX/voicevox_vvm/blob/main/README.md)、[VOICEVOX利用規約](https://voicevox.hiroshiba.jp/term/) |
| 冥鳴ひまり | 推奨音声。クレジットは `VOICEVOX:冥鳴ひまり`。アプリ画面に表示します。録音等を公開する場合も必要な表示を行ってください。[話者公式規約](https://www.meimeihimari.com/terms-of-use) |
| Ollama | MIT。[LICENSE](https://github.com/ollama/ollama/blob/main/LICENSE) |
| Qwen3-4B-Instruct-2507 | Apache 2.0。[モデル](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507)、[LICENSE](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507/blob/main/LICENSE) |
| Open-Meteo | 天気データの出典を画面に表示。CC BY 4.0、無料APIは非商用用途等の条件があります。[利用条件](https://open-meteo.com/en/terms) |
| ニュースRSS | 初期設定はITmedia NEWS。私的利用を想定し、配信元と見出しを表示・読み上げます。記事全文は取得しません。[RSS](https://corp.itmedia.co.jp/media/rss_list/)、[利用条件](https://corp.itmedia.co.jp/media/image/) |
| YouTube | 公式埋め込みプレーヤーを使用します。[開発者ポリシー](https://developers.google.com/youtube/terms/developer-policies) |

本アプリ独自コードへ外部ソフトウェアのライセンスを一括適用するものではありません。各ソフトウェアやモデルを別途再配布する場合は、その対象に適用される表示・ライセンス・話者条件を確認してください。

ユーザーの音楽、設定、履歴、生成音声、ログ、ランタイムの所有プロセス記録は配布ZIPに含めません。
