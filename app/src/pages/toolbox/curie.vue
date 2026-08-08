<template>
  <v-container fluid class="pa-4">
    <v-row class="mb-3" align="center">
      <v-col class="text-center">
        <span class="text-h5 font-weight-bold">{{ $t('curie.title') }}</span>
      </v-col>
      <v-col cols="auto">
        <v-btn small color="error" @click="$router.go(-1)">
          <v-icon small left>mdi-close</v-icon>{{ $t('curie.close') }}
        </v-btn>
      </v-col>
    </v-row>

    <v-row justify="center">
      <v-col cols="12" md="8" lg="6">
        <v-card rounded="xl" outlined class="mt-card pa-6">
          <v-alert type="warning" dense text rounded="lg" class="mb-5">
            {{ $t('curie.hint') }}
          </v-alert>

          <v-text-field
            v-model="query"
            :label="$t('curie.selectBook')"
            :loading="searching"
            outlined
            dense
            clearable
            hide-details
            class="mb-3"
            prepend-inner-icon="mdi-magnify"
            @keyup.enter="search"
            @click:clear="clearSearch"
          />

          <div class="mt-book-list mb-4">
            <div v-if="searching" class="text-center py-6">
              <v-progress-circular indeterminate color="primary" size="32" />
            </div>
            <div v-else-if="books.length === 0 && searched" class="text-center py-4 grey--text">
              {{ $t('curie.noResults') }}
            </div>
            <v-list v-else-if="books.length > 0" dense class="mt-list pa-0">
              <v-list-item
                v-for="book in books"
                :key="book.id"
                :class="['mt-book-item', { 'mt-book-selected': selected && selected.id === book.id }]"
                @click="selectBook(book)"
              >
                <v-list-item-avatar tile size="44" class="mr-3">
                  <v-img :src="book.thumb" :alt="book.title">
                    <template #error>
                      <v-icon color="grey lighten-1">mdi-book-outline</v-icon>
                    </template>
                  </v-img>
                </v-list-item-avatar>
                <v-list-item-content>
                  <v-list-item-title class="mt-book-title">{{ book.title }}</v-list-item-title>
                  <v-list-item-subtitle class="mt-book-author">{{ (book.authors || []).join(', ') }}</v-list-item-subtitle>
                  <div class="mt-1">
                    <v-chip
                      v-for="file in (book.files || [])"
                      :key="file.format"
                      x-small
                      :color="file.format === 'EPUB' ? 'primary' : 'default'"
                      outlined
                      class="mr-1"
                    >{{ file.format }}</v-chip>
                  </div>
                </v-list-item-content>
                <v-list-item-action v-if="selected && selected.id === book.id">
                  <v-icon color="primary">mdi-check-circle</v-icon>
                </v-list-item-action>
              </v-list-item>
            </v-list>
          </div>

          <template v-if="selected">
            <v-divider class="mb-4" />

            <v-select
              v-model="provider"
              :label="$t('curie.provider')"
              :items="providerOptions"
              item-text="label"
              item-value="value"
              outlined
              dense
              hide-details
              class="mb-3"
              prepend-inner-icon="mdi-cloud-outline"
              @change="onProviderChange"
            />

            <v-text-field
              v-model="apiUrl"
              :label="$t('curie.apiUrl')"
              :placeholder="$t('curie.apiUrlPlaceholder')"
              outlined
              dense
              hide-details
              class="mb-3"
              prepend-inner-icon="mdi-link-variant"
            />

            <v-text-field
              v-model="modelName"
              :label="$t('curie.model')"
              :placeholder="$t('curie.modelPlaceholder')"
              outlined
              dense
              hide-details
              class="mb-3"
              prepend-inner-icon="mdi-chip"
            />

            <v-text-field
              v-model="apiKey"
              :label="$t('curie.apiKey')"
              :placeholder="$t('curie.apiKeyPlaceholder')"
              outlined
              dense
              hide-details
              class="mb-4"
              prepend-inner-icon="mdi-key-variant"
              type="password"
            />

            <v-row class="mb-2">
              <v-col cols="6">
                <v-switch
                  v-model="includeCharacters"
                  :label="$t('curie.characters')"
                  dense
                  inset
                  color="primary"
                  hide-details
                />
              </v-col>
              <v-col cols="6">
                <v-switch
                  v-model="includePlaces"
                  :label="$t('curie.places')"
                  dense
                  inset
                  color="primary"
                  hide-details
                />
              </v-col>
            </v-row>

            <v-select
              v-model="language"
              :label="$t('curie.language')"
              :items="languageOptions"
              item-text="label"
              item-value="value"
              outlined
              dense
              hide-details
              class="mb-3"
              prepend-inner-icon="mdi-translate"
            />

            <v-select
              v-model="hintDensity"
              :label="$t('curie.density')"
              :items="densityOptions"
              item-text="label"
              item-value="value"
              outlined
              dense
              hide-details
              class="mb-3"
              prepend-inner-icon="mdi-format-paragraph"
            />

            <transition name="mt-fade">
              <v-alert
                v-if="resultMsg"
                :type="resultType"
                dense
                text
                rounded="lg"
                class="mb-4"
              >{{ resultMsg }}</v-alert>
            </transition>

            <div class="d-flex justify-center flex-wrap">
              <v-btn
                color="secondary"
                outlined
                class="mt-test-btn mx-2"
                :loading="testing"
                :disabled="!apiKey.trim() || !modelName.trim()"
                @click="testConnection"
              >
                <v-icon left>mdi-connection</v-icon>
                {{ testing ? $t('curie.testTesting') : $t('curie.testBtn') }}
              </v-btn>

              <v-btn
                color="primary"
                class="mt-start-btn mx-2"
                :loading="processing"
                :disabled="processing || !canConvert"
                @click="startConvert"
              >
                <v-icon left>mdi-book-open-page-variant</v-icon>
                {{ $t('curie.startBtn') }}
              </v-btn>

              <v-btn
                color="secondary"
                outlined
                class="mt-regen-btn mx-2"
                :loading="processing"
                :disabled="processing || !preview"
                @click="startRegenerate"
              >
                <v-icon left>mdi-refresh</v-icon>
                {{ $t('curie.regenerateBtn') }}
              </v-btn>
            </div>

            <div v-if="processing || completed" class="mt-4">
              <div class="mb-1 d-flex justify-space-between text-caption">
                <span>{{ status === 'completed' ? $t('curie.statusCompleted') : $t('curie.statusProcessing') }}</span>
                <span>{{ progress }}%</span>
              </div>
              <v-progress-linear v-model="progress" height="10" rounded color="primary" />
              <div v-if="processing" class="d-flex justify-center mt-2">
                <v-btn
                  small
                  outlined
                  color="error"
                  :loading="cancelling"
                  @click="cancelTask"
                >
                  <v-icon left small>mdi-cancel</v-icon>
                  {{ $t('curie.cancelBtn') }}
                </v-btn>
              </div>
            </div>

            <div v-if="completed && newBookId" class="d-flex justify-center mt-4">
              <v-btn
                color="success"
                outlined
                @click="$router.push('/book/' + newBookId)"
              >
                <v-icon left>mdi-book-open-variant</v-icon>
                {{ $t('curie.goBook') }}
              </v-btn>
            </div>

            <template v-if="preview && preview.characters">
              <v-divider class="my-4" />
              <div class="text-subtitle-2 mb-2">
                {{ $t('curie.preview') }}
                <span class="text-caption grey--text">
                  （{{ preview.characters.length }} {{ $t('curie.previewCharacters') }} / {{ preview.locations.length }} {{ $t('curie.previewLocations') }}）
                </span>
              </div>
              <v-expansion-panels dense class="mb-2">
                <v-expansion-panel v-for="(ch, i) in preview.characters" :key="'c' + i">
                  <v-expansion-panel-header class="text-subtitle-2">
                    {{ ch.full_name || ch.name }}
                    <span class="text-caption grey--text ml-2">
                      {{ ch.role || '' }} · {{ $t('curie.chapter') }} {{ ch.chapter || '-' }} · {{ $t('curie.occurrences') }} {{ ch.occurrences || 0 }}
                    </span>
                  </v-expansion-panel-header>
                  <v-expansion-panel-content class="text-caption">
                    {{ ch.description || '' }}
                  </v-expansion-panel-content>
                </v-expansion-panel>
              </v-expansion-panels>
              <v-expansion-panels dense>
                <v-expansion-panel v-for="(loc, i) in preview.locations" :key="'l' + i">
                  <v-expansion-panel-header class="text-subtitle-2">
                    {{ loc.name || loc.full_name }}
                    <span class="text-caption grey--text ml-2">
                      {{ loc.type || '' }} · {{ $t('curie.chapter') }} {{ loc.chapter || '-' }} · {{ $t('curie.occurrences') }} {{ loc.occurrences || 0 }}
                    </span>
                  </v-expansion-panel-header>
                  <v-expansion-panel-content class="text-caption">
                    {{ loc.description || '' }}
                  </v-expansion-panel-content>
                </v-expansion-panel>
              </v-expansion-panels>
            </template>
          </template>
        </v-card>
      </v-col>
    </v-row>
  </v-container>
</template>

<script>
const PROVIDER_PRESETS = {
  anthropic: {
    url: 'https://api.anthropic.com',
    model: 'claude-sonnet-4-6',
  },
  openai_compat: {
    url: 'https://api.deepseek.com',
    model: 'deepseek-chat',
  },
};

export default {
  data: () => ({
    query: '',
    books: [],
    searching: false,
    searched: false,
    selected: null,

    provider: 'anthropic',
    apiUrl: PROVIDER_PRESETS.anthropic.url,
    modelName: PROVIDER_PRESETS.anthropic.model,
    apiKey: '',
    includeCharacters: true,
    includePlaces: false,
    language: 'auto',
    hintDensity: 'every_10_paragraphs',

    processing: false,
    testing: false,
    cancelling: false,
    resultMsg: '',
    resultType: 'success',
    completed: false,
    progress: 0,
    status: '',
    newBookId: 0,
    preview: null,
    pollInterval: null,
    _previewSeq: 0,
  }),
  computed: {
    providerOptions() {
      const t = this.$t.bind(this);
      return [
        { value: 'anthropic', label: t('curie.providerAnthropic') },
        { value: 'openai_compat', label: t('curie.providerOpenAI') },
      ];
    },
    languageOptions() {
      const t = this.$t.bind(this);
      return [
        { value: 'auto', label: t('curie.langAuto') },
        { value: 'Chinese', label: t('curie.langChinese') },
        { value: 'English', label: t('curie.langEnglish') },
        { value: 'Svenska', label: t('curie.langSwedish') },
      ];
    },
    densityOptions() {
      const t = this.$t.bind(this);
      return [
        { value: 'every_mention', label: t('curie.densityEveryMention') },
        { value: 'every_10_paragraphs', label: t('curie.densityEvery10') },
        { value: 'once_per_chapter', label: t('curie.densityOncePerChapter') },
      ];
    },
    canConvert() {
      return (
        this.selected &&
        (this.selected.files || []).some((f) => f.format === 'EPUB') &&
        this.apiKey.trim() &&
        this.modelName.trim() &&
        (this.includeCharacters || this.includePlaces)
      );
    },
  },
  async created() {
    this.$store.commit('navbar', true);
    await this.loadSavedConfig();
  },
  beforeDestroy() {
    if (this.pollInterval) {
      clearInterval(this.pollInterval);
      this.pollInterval = null;
    }
  },
  methods: {
    onProviderChange(type) {
      const preset = PROVIDER_PRESETS[type];
      if (preset) {
        this.apiUrl = preset.url;
        this.modelName = preset.model;
      }
    },
    async loadSavedConfig() {
      try {
        const rsp = await this.$backend('/toolbox/curie/config');
        if (rsp.err === 'ok' && rsp.config) {
          const c = rsp.config;
          this.apiKey = c.api_key || '';
          if (c.provider) {
            this.provider = c.provider;
            const preset = PROVIDER_PRESETS[c.provider];
            this.apiUrl = c.api_url || (preset ? preset.url : '');
            this.modelName = c.model || (preset ? preset.model : '');
          }
          this.resultMsg = this.$t('curie.configLoaded');
          this.resultType = 'info';
        }
      } catch (_e) {
      }
    },
    async testConnection() {
      this.testing = true;
      this.resultMsg = '';
      this.completed = false;
      try {
        const rsp = await this.$backend('/toolbox/curie/test', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            provider: this.provider,
            api_key: this.apiKey.trim(),
            model: this.modelName.trim(),
            api_url: this.apiUrl.trim(),
          }),
        });
        if (rsp.err === 'ok') {
          this.resultMsg = rsp.msg || this.$t('curie.testSuccess');
          this.resultType = 'success';
        } else {
          this.resultMsg = rsp.msg || rsp.err;
          this.resultType = 'error';
        }
      } catch (e) {
        this.resultMsg = String(e);
        this.resultType = 'error';
      } finally {
        this.testing = false;
      }
    },
    async search() {
      const q = (this.query || '').trim();
      if (!q) return;
      this.searching = true;
      this.searched = false;
      this.selected = null;
      this.preview = null;
      try {
        const rsp = await this.$backend(`/search?title=title:${encodeURIComponent(q)}`);
        this.books = rsp.err === 'ok' ? (rsp.books || []) : [];
      } catch (_e) {
        this.books = [];
      } finally {
        this.searching = false;
        this.searched = true;
      }
    },
    clearSearch() {
      this.books = [];
      this.selected = null;
      this.searched = false;
      this.preview = null;
      this.resultMsg = '';
      this.completed = false;
    },
    selectBook(book) {
      if (this.selected && this.selected.id === book.id) {
        // 取消选择：不再发起 preview 请求
        this.selected = null;
        this.resultMsg = '';
        this.completed = false;
        this.preview = null;
        return;
      }
      this.selected = book;
      this.resultMsg = '';
      this.completed = false;
      this.preview = null;
      this.loadPreview(book.id);
    },
    async loadPreview(bookId) {
      const seq = ++this._previewSeq;
      try {
        const rsp = await this.$backend('/toolbox/curie/preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ book_id: bookId }),
        });
        if (seq !== this._previewSeq) return; // 已被更新的请求取代
        if (rsp.err === 'ok' && rsp.data) {
          this.preview = rsp.data;
        }
      } catch (_e) {
      }
    },
    async startConvert() {
      if (!this.canConvert) return;
      this.resultMsg = '';
      this.completed = false;
      this.processing = true;
      this.progress = 0;
      this.status = '';
      this.newBookId = 0;
      try {
        const rsp = await this.$backend('/toolbox/curie/convert', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            book_id: this.selected.id,
            provider: this.provider,
            api_key: this.apiKey.trim(),
            model: this.modelName.trim(),
            api_url: this.apiUrl.trim(),
            include_characters: this.includeCharacters,
            include_places: this.includePlaces,
            language: this.language,
            hint_density: this.hintDensity,
          }),
        });
        if (rsp.err === 'ok') {
          this.resultMsg = rsp.msg || this.$t('curie.convertStarted');
          this.resultType = 'success';
          this.pollProgress();
        } else {
          this.resultMsg = rsp.msg || rsp.err;
          this.resultType = 'error';
          this.processing = false;
        }
      } catch (e) {
        this.resultMsg = String(e);
        this.resultType = 'error';
        this.processing = false;
      }
    },
    async startRegenerate() {
      if (!this.selected || !this.preview) return;
      this.resultMsg = '';
      this.completed = false;
      this.processing = true;
      this.progress = 0;
      this.status = '';
      this.newBookId = 0;
      try {
        const rsp = await this.$backend('/toolbox/curie/regenerate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            book_id: this.selected.id,
            hint_density: this.hintDensity,
          }),
        });
        if (rsp.err === 'ok') {
          this.resultMsg = rsp.msg || this.$t('curie.convertStarted');
          this.resultType = 'success';
          this.pollProgress();
        } else {
          this.resultMsg = rsp.msg || rsp.err;
          this.resultType = 'error';
          this.processing = false;
        }
      } catch (e) {
        this.resultMsg = String(e);
        this.resultType = 'error';
        this.processing = false;
      }
    },
    async cancelTask() {
      if (!this.processing) return;
      this.cancelling = true;
      try {
        const rsp = await this.$backend('/toolbox/curie/cancel', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
        });
        if (rsp.err === 'ok') {
          this.resultMsg = rsp.msg || this.$t('curie.cancelRequested');
          this.resultType = 'info';
        } else {
          this.resultMsg = rsp.msg || rsp.err;
          this.resultType = 'error';
        }
      } catch (e) {
        this.resultMsg = String(e);
        this.resultType = 'error';
      } finally {
        this.cancelling = false;
      }
    },
    pollProgress() {
      if (this.pollInterval) {
        clearInterval(this.pollInterval);
      }
      this.pollInterval = setInterval(async () => {
        try {
          const rsp = await this.$backend('/toolbox/curie/progress');
          if (rsp.err === 'ok' && rsp.data) {
            this.progress = rsp.data.progress || 0;
            this.status = rsp.data.status || '';
            if (rsp.data.status === 'completed') {
              clearInterval(this.pollInterval);
              this.pollInterval = null;
              this.processing = false;
              this.completed = true;
              this.newBookId = rsp.data.new_book_id || 0;
              this.resultMsg = rsp.msg || this.$t('curie.convertCompleted');
              this.resultType = 'success';
              this.loadPreview(this.selected.id);
            } else if (rsp.data.status === 'cancelled') {
              clearInterval(this.pollInterval);
              this.pollInterval = null;
              this.processing = false;
              this.resultMsg = rsp.msg || this.$t('curie.taskCancelled');
              this.resultType = 'info';
            } else if (rsp.data.status === 'failed') {
              clearInterval(this.pollInterval);
              this.pollInterval = null;
              this.processing = false;
              this.resultMsg = rsp.msg || this.$t('curie.convertFailed');
              this.resultType = 'error';
            }
          } else {
            clearInterval(this.pollInterval);
            this.pollInterval = null;
            this.processing = false;
            this.resultMsg = rsp.msg || rsp.err;
            this.resultType = 'error';
          }
        } catch (e) {
          clearInterval(this.pollInterval);
          this.pollInterval = null;
          this.processing = false;
          this.resultMsg = String(e);
          this.resultType = 'error';
        }
      }, 2000);
    },
  },
};
</script>

<style scoped>
.mt-card {
  border: 2px solid #90CAF9;
}

.mt-book-list {
  max-height: 320px;
  overflow-y: auto;
}

.mt-list {
  background: transparent !important;
}

.mt-book-item {
  border-radius: 8px !important;
  margin-bottom: 4px;
  cursor: pointer;
  transition: background 0.15s;
}

.mt-book-item:hover {
  background: rgba(144, 202, 249, 0.15) !important;
}

.mt-book-selected {
  background: rgba(144, 202, 249, 0.25) !important;
  border: 1px solid #90CAF9;
}

.mt-book-title {
  font-size: 13px !important;
  white-space: normal !important;
  line-height: 1.3;
}

.mt-book-author {
  font-size: 11px !important;
}

.mt-start-btn {
  min-width: 180px;
}

.mt-test-btn {
  min-width: 140px;
}

.mt-regen-btn {
  min-width: 140px;
}

.mt-fade-enter-active,
.mt-fade-leave-active {
  transition: opacity 0.3s, transform 0.25s;
}
.mt-fade-enter,
.mt-fade-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
</style>
