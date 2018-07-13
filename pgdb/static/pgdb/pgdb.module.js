var env = {};

// Import variables if present (from env.js)
if (window) {
    Object.assign(env, window.__env);
}

var app = angular.module('pgdb', [
    'ui.router',
    //'long2know',
    'ngResource',
    'ngFileSaver',
    'ui.bootstrap',
    'ngCookies',
    'ngMaterial',
    'angular-mousetrap',
    'databaseList',
    'databaseDetail',
    'corpusDetail',
    'bestiaryPlot',
    'queryDetail',
    'query',
    'enrichment',
    'acoustics',
    'navbar',
    'login',
    'logout',
    'subset'
]).run(
    function ($http, $cookies) {
        //$http.defaults.headers.post['X-CSRFToken'] = $cookies.get('csrftoken');
        //console.log($http.defaults.headers.post)
        // Add the following two lines
        $http.defaults.xsrfCookieName = 'csrftoken';
        $http.defaults.xsrfHeaderName = 'X-CSRFToken';
    });


app.constant('__env', env);

app.config(function ($stateProvider, $urlRouterProvider) {
    $stateProvider
        .state('home', {
            url: '/',
            template: '<div></div>'
        })
        .state('database-list', {
            url: '/databases',
            templateUrl: static('pgdb/database-list/database_list.html'),
            controller: 'DatabaseListCtrl'
        }).state('database-detail', {
        url: '/databases/{database_id:int}',
        templateUrl: static('pgdb/database-detail/database_detail.html'),
        controller: 'DatabaseDetailCtrl'
    }).state('corpus-detail', {
        url: '/corpora/:corpus_id',
        templateUrl: static('pgdb/corpus-detail/corpus_detail.html'),
        controller: 'CorpusDetailCtrl'
    }).state('enrichment', {
        url: '/corpora/:corpus_id/enrichment',
        templateUrl: static('pgdb/enrichment/enrichment.html'),
        controller: 'EnrichmentCtrl'
    }).state('bestiary-plot', {
        url: '/bestiary/:corpus_id',
        templateUrl: static('intonation/bestiary-plot/bestiary_plot.html'),
        controller: 'BestiaryPlotCtrl'
    }).state('utterance-detail', {
        url: '/utterances/:corpus_id/:utterance_id',
        templateUrl: static('pgdb/utterance-detail/utterance_detail.html'),
        controller: 'UtteranceDetailCtrl'
    }).state('query', {
        url: '/query/:corpus_id/{query_id:int}',
        templateUrl: static('pgdb/query/query.html'),
        controller: 'QueryCtrl'
    }).state('new_query', {
        url: '/query/:corpus_id/:type',
        templateUrl: static('pgdb/query/query.html'),
        controller: 'NewQueryCtrl'
    }).state('query-detail', {
        url: '/query_results/:corpus_id/:query_id/:detail_index',
        templateUrl: static('pgdb/query-detail/query_detail.html'),
        controller: 'QueryDetailCtrl'
    }).state('edit_subset', {
        url: '/subset/:corpus_id/{enrichment_id:int}',
        templateUrl: static('pgdb/subset/subset.html'),
        controller: 'NewSubsetCtrl'
    }).state('new_subset', {
        url: '/subset/:corpus_id/:type',
        templateUrl: static('pgdb/subset/subset.html'),
        controller: 'NewSubsetCtrl'
    }).state('check', {
            url: '/check'
        })
        .state('login', {
            url: '/login',
            templateUrl: static('pgdb/login/login.html'),
            controller: 'LoginCtrl',
            resolve: {
            }
        }).state('logout',{
            url: '/logout',
            templateUrl: static('pgdb/logout/logout.html'),
            controller: 'LogoutCtrl',
            resolve: {
            }
        }).state('acoustic_enrichment', {
        url: '/corpora/:corpus_id/enrichment/acoustics',
        templateUrl: static('pgdb/enrichment/acoustics/acoustics.html'),
        controller: 'AcousticCtrl'
    });

    $urlRouterProvider.otherwise('/');
});