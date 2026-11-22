  function project(tweet) {
    if (tweet?.__typename === "TweetWithVisibilityResults") tweet = tweet.tweet;
    const legacy = tweet?.legacy;
    const user = tweet?.core?.user_results?.result;
    const screen_name = user?.core?.screen_name || user?.legacy?.screen_name;
    const note = tweet?.note_tweet?.note_tweet_results?.result;
    const full_text = note?.text || legacy?.full_text;
    const repost = legacy?.retweeted_status_result?.result;
    const original = repost?.tweet || repost;
    const urls = [...(original?.legacy?.entities?.urls || []), ...(legacy?.entities?.urls || []),
      ...(note?.entity_set?.urls || [])]
      .map(({url, expanded_url}) => ({url, expanded_url}));
    if (!legacy || !screen_name || !full_text || !/^\d+$/.test(tweet.rest_id)) return null;
    const media = (legacy.extended_entities?.media || []).map(m => ({
      url: m.url, expanded_url: m.expanded_url, type: m.type, media_url_https: m.media_url_https, video_info: {
        duration_millis: m.video_info?.duration_millis,
        variants: (m.video_info?.variants || []).map(({content_type, bitrate, url}) => ({content_type, bitrate, url}))
      }
    }));
    return {rest_id: tweet.rest_id, core: {user_results: {result: {core: {screen_name}}}},
      views: {count: tweet.views?.count}, quoted_status_result: {result: project(tweet.quoted_status_result?.result)},
      legacy: {full_text, entities: {urls}, extended_entities: {media}, created_at: legacy.created_at, lang: legacy.lang,
        favorite_count: legacy.favorite_count, retweet_count: legacy.retweet_count, reply_count: legacy.reply_count,
        quote_count: legacy.quote_count, in_reply_to_status_id_str: legacy.in_reply_to_status_id_str,
        conversation_id_str: legacy.conversation_id_str}};
  }
